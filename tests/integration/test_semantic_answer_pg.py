"""Opt-in PostgreSQL semantic answer custody through HTTP and the actual worker.

FunctionModel, a component embedding fake and CapturedQueue replace external
inference and broker effects. Explicit typed checkpoint reconstruction does not
claim automatic recovery after worker death or actual native model execution.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart
from pydantic_graph import GraphRunContext
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lychd.agents.workflows import BuiltinWorkflowRegistry
from lychd.agents.workflows.semantic_chat import (
    ARCHIVE_ANSWER_PURPOSE,
    BRIDGE_CHAT_SEMANTIC,
    SEMANTIC_CHAT_GRAPH,
    AnswerSemanticNotes,
    RecallSemanticNotes,
    SemanticChatState,
    SettleSemanticAnswer,
    semantic_recall_request,
)
from lychd.db.archive_derivation import DbArchiveDerivationStore
from lychd.db.archive_semantic import DbArchiveSemanticStore
from lychd.db.checkpoints import PostgresStasisStore
from lychd.domain.animation.capabilities import CapabilityGrant, SourceKind
from lychd.domain.animation.embeddings import EmbeddingBinding
from lychd.domain.animation.schemas.capability_family import CapabilityFamily
from lychd.domain.archive import ArchiveRefused, ArchiveService, text_digest
from lychd.domain.archive.derivation import DerivationRequest, GenerationManifest, GenerationRef
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.domain.archive.semantic_service import ArchiveSemanticService
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.ledger import DbRunLedger
from lychd.domain.cortex.runs import RunStatus
from lychd.domain.cortex.semantic_answer import SemanticAnswerBinding
from lychd.domain.cortex.stasis import DurableStasisPhylactery, NodeSnapshot
from tests.agents.fakes import FakeDispatcher
from tests.integration import test_archive_semantic_pg as semantic_pg
from tests.integration import test_archive_vertical_pg as vertical
from tests.support.archive_derivation import Embeddings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.container]
pg_url = vertical.pg_url
pg_factory = vertical.pg_factory


class LocalChat(FakeDispatcher):
    """Enforce local chat metadata and joined query cleanup at chat acquisition."""

    def __init__(self, model: Any, queries: semantic_pg.Queries) -> None:
        super().__init__(model=model, key=vertical.CAPABILITY)
        self.queries = queries

    @asynccontextmanager
    async def lease_grant(self, **kwargs: Any) -> AsyncGenerator[CapabilityGrant]:
        assert self.queries.calls == self.queries.closed == 1
        async with super().lease_grant(**kwargs) as grant:
            grant.spec.family = CapabilityFamily.CHAT
            grant.spec.source_kind = SourceKind.SOULSTONE
            yield grant


class SemanticBoot(vertical.PostgresArchiveBoot):
    """Reuse the PostgreSQL vertical composition, changing only the semantic slice."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        binding: SemanticAnswerBinding | None = None,
        queries: semantic_pg.Queries | None = None,
    ) -> None:
        super().__init__(factory)
        self.policy = replace(self.policy, derivations=True, semantic=True)
        self.archive = ArchiveService(profile=vertical.PROFILE, store=self.store, authorizer=self.policy)
        self.queries = queries or semantic_pg.Queries()
        manifest = GenerationManifest(
            archive=vertical.PROFILE,
            binding=EmbeddingBinding(capability_key="local:embedding:fixed", profile_digest=semantic_pg.PROFILE.digest),
            embedding=semantic_pg.PROFILE,
        )
        self.binding = binding or SemanticAnswerBinding(
            min_cosine=0.25,
            manifest=manifest,
            generation=GenerationRef(generation_id=uuid4(), manifest_digest=manifest.digest),
        )
        self.derivation = ArchiveDerivationService(
            generation_id=self.binding.generation.generation_id,
            manifest=self.binding.manifest,
            store=DbArchiveDerivationStore(factory),
            authorizer=self.policy,
            dispatcher=Embeddings(),
        )
        self.semantic = ArchiveSemanticService(
            generation_id=self.binding.generation.generation_id,
            manifest=self.binding.manifest,
            store=DbArchiveSemanticStore(factory, generation_id=self.binding.generation.generation_id),
            authorizer=self.policy,
            dispatcher=self.queries,
        )
        self.dispatcher = LocalChat(self.model, self.queries)
        self.workflows = BuiltinWorkflowRegistry(workflows=(BRIDGE_CHAT_SEMANTIC,))
        self.checkpoints = PostgresStasisStore(factory)
        self.substrate = replace(
            self.substrate,
            workflows=self.workflows,
            dispatcher=self.dispatcher,
            stasis_store=self.checkpoints,
            archive=self.archive,
            archive_semantic=self.semantic,
            archive_subject_resolver=self.policy.subject_for,
        )
        self.engine = RunEngine(
            ledger=self.ledger,
            bus=self.bus,
            workflows=self.workflows,
            queue_router=QueueRouter({"default": RouteRule(queue="runs", priority=70)}),
            queues=self.substrate.queues,
            stasis_store=self.checkpoints,
            consents=self.consents,
            release_context=self.substrate.context.release,
            bridge_capability_key=vertical.CAPABILITY,
            bridge_semantic=self.binding,
        )


async def seed(boot: SemanticBoot) -> UUID:
    note = await vertical.promote(boot)
    subject = vertical.PROFILE.owner_subject
    await boot.derivation.register(subject, boot.derivation.reference, purpose="archive:derive:manage")
    current = await boot.archive.get_note(subject, UUID(note["reference"]["record_id"]), purpose="archive:manage")
    result = await boot.derivation.derive(
        subject,
        DerivationRequest(generation=boot.derivation.reference, source=current.reference),
        purpose="archive:derive",
    )
    assert result.state == "complete"
    return current.reference.record_id


async def admit(
    boot: SemanticBoot, *, session_id: str | None = None, request_id: str | None = None
) -> tuple[str, str, str]:
    request_id = request_id or str(uuid4())
    async with boot.client() as client:
        if session_id is None:
            session_id = (await vertical.post(client, "/api/v1/bridge/sessions", {}))["session"]["id"]
        admitted = await vertical.post(
            client,
            f"/api/v1/bridge/sessions/{session_id}/messages",
            {"prompt": "  Which quartz note supports this?\n", "request_id": request_id},
            status=200,
        )
    assert admitted["pattern_id"] == "bridge_chat"
    assert admitted["pattern_revision"] == "5"
    assert isinstance(session_id, str)
    return session_id, admitted["run_id"], request_id


def assert_transient(document: str) -> None:
    assert "ARCHIVE_SOURCE_BYTES_SENTINEL" not in document
    assert "instruction_authority" not in document
    assert '"vector":' not in document
    assert '"query_vector":' not in document


async def test_retained_admission_worker_schema2_and_ordinary_settlement(  # noqa: PLR0915 - one ordered admission/worker/settlement custody proof
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = SemanticBoot(pg_factory)
    await seed(original)
    session_id, run_id, request_id = await admit(original)
    assert original.queries.calls == original.queries.closed == 0
    retained = await DbRunLedger(session_factory=pg_factory).get(run_id)
    assert retained is not None
    assert retained.admitted_semantic == original.binding
    assert retained.to_intent().admitted_semantic == original.binding
    assert retained.admitted_capability_key == vertical.CAPABILITY
    assert retained.pattern_manifest == BRIDGE_CHAT_SEMANTIC.manifest.snapshot()
    fresh = SemanticBoot(pg_factory, binding=original.binding, queries=original.queries)
    try:
        fresh.engine.bridge_semantic = original.binding.model_copy(update={"min_cosine": 0.9})
        fresh.engine.bridge_capability_key = "changed:chat:configuration"
        assert (await admit(fresh, session_id=session_id, request_id=request_id))[1] == run_id
        await fresh.drive(run_id)
        finished = await DbRunLedger(session_factory=pg_factory).get(run_id)
        assert finished is not None
        assert finished.status is RunStatus.DONE
        assert finished.admitted_semantic == original.binding
        assert fresh.queries.calls == fresh.queries.closed == 1
        assert len(fresh.outbound) == 1
        wire = vertical.envelope(fresh.outbound[0])
        assert wire["kind"] == "archive_semantic_recall"
        assert wire["instruction_authority"] == "none"
        assert wire["receipt"]["schema_version"] == 2
        assert wire["receipt"]["generation"] == original.binding.generation.model_dump(mode="json")
        assert wire["receipt"]["coverage"]["promoted_notes"] == wire["receipt"]["coverage"]["eligible_notes"] == 1
        assert (wire["receipt"]["min_cosine"], wire["receipt"]["max_results"], wire["receipt"]["char_budget"]) == (
            0.25,
            5,
            8000,
        )
        assert wire["receipt"]["omitted_for_budget"] == 0
        assert wire["items"][0]["text"] == vertical.SOURCE
        assert wire["items"][0]["selection"]["source_key"] == "public-note"
        assert wire["items"][0]["selection"]["source_revision"] == "revision-1"
        assert fresh.model_info[0].function_tools == []
        assert vertical.SOURCE not in str(fresh.model_info[0].instructions)
        assert fresh.dispatcher.requires_tools_calls == [False]
        assert fresh.dispatcher.capability_key_calls == [vertical.CAPABILITY]
        # Ordinary linear execution uses Live Stasis; the explicit SQL restoration test below is separate.
        assert await fresh.checkpoints.load(run_id) is None
        history = await fresh.sessions.get_session(session_id)
        assert history is not None
        assert vertical.FIRST_ANSWER in json.dumps(history.message_history)
        assert_transient(json.dumps(history.message_history))
        state = BRIDGE_CHAT_SEMANTIC.make_state(finished.to_intent())
        assert isinstance(state, SemanticChatState)
        await RecallSemanticNotes().run(GraphRunContext(state=state, deps=fresh.services()))
        assert str(state.receipt_id) == wire["receipt"]["receipt_id"]
        assert state.receipt_digest == wire["receipt"]["receipt_digest"]
        assert state.current_query_execution is None
        assert fresh.queries.calls == fresh.queries.closed == 1
    finally:
        await original.bus.aclose()
        await fresh.bus.aclose()


@pytest.mark.parametrize("change", ["unchanged", "revoke", "retire", "service_generation"])
async def test_postgres_checkpoint_reconstruction_rechecks_before_chat(  # noqa: PLR0915 - one ordered typed restoration and current-authority proof
    pg_factory: async_sessionmaker[AsyncSession], change: str
) -> None:
    original = SemanticBoot(pg_factory)
    note_id = await seed(original)
    session_id, run_id, _ = await admit(original)
    retained = await original.ledger.get(run_id)
    assert retained is not None
    state = BRIDGE_CHAT_SEMANTIC.make_state(retained.to_intent())
    assert isinstance(state, SemanticChatState)
    await RecallSemanticNotes().run(GraphRunContext(state=state, deps=original.services()))
    assert state.receipt_id is not None
    assert state.receipt_digest is not None
    assert state.current_query_execution is not None
    persistence = DurableStasisPhylactery(job_id=run_id, store=original.checkpoints)
    persistence.set_graph_types(SEMANTIC_CHAT_GRAPH)
    await persistence.snapshot_node(state, AnswerSemanticNotes())
    assert_transient(json.dumps(await original.checkpoints.load(run_id)))
    fresh = SemanticBoot(pg_factory, binding=original.binding, queries=original.queries)
    try:
        resumed = DurableStasisPhylactery(job_id=run_id, store=fresh.checkpoints)
        resumed.set_graph_types(SEMANTIC_CHAT_GRAPH)
        snapshots = await resumed.load_all()
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert isinstance(snapshot, NodeSnapshot)
        assert isinstance(snapshot.state, SemanticChatState)
        assert isinstance(snapshot.node, AnswerSemanticNotes)
        assert snapshot.state == state
        assert snapshot.state.semantic == original.binding
        assert snapshot.state.current_query_execution == state.current_query_execution
        assert BRIDGE_CHAT_SEMANTIC.validate_state is not None
        reloaded = await fresh.ledger.get(run_id)
        assert reloaded is not None
        BRIDGE_CHAT_SEMANTIC.validate_state(reloaded.to_intent(), snapshot.state)
        subject = vertical.PROFILE.owner_subject
        request = semantic_recall_request(binding=original.binding, run_id=run_id, prompt=state.prompt)
        receipt = await fresh.semantic.inspect_recall(
            subject, request, recall_purpose=ARCHIVE_ANSWER_PURPOSE, purpose="archive:manage"
        )
        assert receipt is not None
        assert receipt.request_id == request.request_id
        assert receipt.run_id == UUID(run_id)
        assert receipt.query_digest == text_digest(state.prompt)
        assert receipt.request_digest == request.binding_digest(
            original.binding.manifest, subject, ARCHIVE_ANSWER_PURPOSE
        )
        assert (snapshot.state.receipt_id, snapshot.state.receipt_digest) == (
            receipt.receipt_id,
            receipt.receipt_digest,
        )
        assert snapshot.state.current_query_execution == receipt.query_execution
        if change == "revoke":
            await original.archive.revoke(subject, note_id, expected_version=2, purpose="archive:manage")
        elif change == "retire":
            await original.derivation.retire(
                subject, original.derivation.reference, expected_version=1, purpose="archive:derive:manage"
            )
        elif change == "service_generation":
            changed = uuid4()
            fresh.substrate.archive_semantic = ArchiveSemanticService(
                generation_id=changed,
                manifest=fresh.binding.manifest,
                store=DbArchiveSemanticStore(pg_factory, generation_id=changed),
                authorizer=fresh.policy,
                dispatcher=fresh.queries,
            )
        ctx = GraphRunContext(state=snapshot.state, deps=fresh.services())
        if change == "unchanged":
            settle = await snapshot.node.run(ctx)
            assert isinstance(settle, SettleSemanticAnswer)
            assert_transient(snapshot.state.model_dump_json())
            users = [
                part.content
                for message in ModelMessagesTypeAdapter.validate_python(snapshot.state.new_messages)
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            assert users == [state.prompt]
            await settle.run(ctx)
            assert len(fresh.outbound) == 1
        else:
            code = {
                "revoke": r"source_integrity|receipt_stale",
                "retire": "generation_retired",
                "service_generation": "casting_binding",
            }
            with pytest.raises(ArchiveRefused, match=code[change]):
                await snapshot.node.run(ctx)
            assert fresh.outbound == []
            assert fresh.dispatcher.calls == []
            session = await fresh.sessions.get_session(session_id)
            assert session is not None
            assert all(turn.role != "agent" for turn in session.turns)
        assert fresh.queries.calls == fresh.queries.closed == 1
    finally:
        await original.bus.aclose()
        await fresh.bus.aclose()
