"""Credential-backed Scout choreography with real PostgreSQL custody.

Only the disposable database is effectful. Fetch uses fixed bytes and synchronous
syscall substitutes; the real extraction supervisor receives a substituted child.
Model inference and queue publication are inert. This does not qualify network,
sandbox, model-service, SAQ-worker, or durable Graph-checkpoint behavior.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_graph import GraphRunContext
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lychd.agents.router import Intent
from lychd.agents.services import WorkflowServices
from lychd.agents.workflows import builtin_workflow_registry
from lychd.agents.workflows.scout_chat import (
    BRIDGE_CHAT_SCOUT,
    AcquireAndAnswerPage,
    AwaitPageConsent,
    PreparePageReview,
    ScoutChatState,
    SettlePageAnswer,
)
from lychd.config.settings import BridgeCastingSettings, WeaverSettings
from lychd.db import scout as scout_db
from lychd.db.models import Consent, Run, RunDelivery, ScoutAttempt, ScoutBudget, Session, Step
from lychd.db.scout import DbScoutAttemptStore
from lychd.db.sessions import DbBridgeSessionStore
from lychd.domain.animation.capabilities import CapabilityGrant, SourceKind
from lychd.domain.animation.schemas.capability_family import CapabilityFamily
from lychd.domain.codex.ledger import CodexConsentLedger
from lychd.domain.codex.local_access import LocalOperatorAccess, LocalOperatorAuthentication
from lychd.domain.codex.sigil import default_local_sigil
from lychd.domain.cortex.cancellation import RunEffectFence
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.events import InProcessEventBus
from lychd.domain.cortex.ledger import DbRunLedger
from lychd.domain.cortex.runs import ConsentPending
from lychd.domain.scout.attempts import ScoutCommitUnknownError, ScoutSettlement
from lychd.domain.scout.authority import ScoutAction, ScoutHostAuthorizer, ScoutHostPolicy
from lychd.domain.scout.destination import DestinationCandidate
from lychd.domain.scout.extraction import ExtractionCode, ExtractionError
from lychd.domain.scout.profile import SCOUT_ANSWER_PURPOSE, ScoutProfile, ScoutWorkflowCode, ScoutWorkflowError
from lychd.domain.scout.transport import FetchError, NetworkGate, NetworkHandoff, NetworkTarget
from lychd.domain.web.consent_reviews import ConsentReviewForbiddenError
from lychd.interface.web.scout_admission import ScoutAdmissionService
from lychd.interface.web.scout_review import ScoutConsentReviews
from lychd.system import scout_worker
from lychd.system.services import scout_extraction, scout_workflow
from lychd.system.services.scout_fetch import FetchedPage, FetchObservation
from lychd.system.services.scout_http import serialize_get
from lychd.system.services.scout_workflow import ScoutCompletions, ScoutWorkflowService
from tests.agents.conftest import make_services
from tests.agents.fakes import FakeConsents, FakeDispatcher, FakeEvents, FakeOrchestrator, FakeTurns
from tests.scout_fixtures import CAPABILITY, scout_profile

pytestmark = [pytest.mark.integration, pytest.mark.container]

_URL = "https://8.8.8.8/page?fixture=1"
_PROMPT = f"/read-public {_URL}\nWhat does the fixed page report?"
_SOURCE = "SCOUT_INTEGRATION_RAW_SOURCE_73b29"
_BODY = f"{_SOURCE}: a fixed fixture claim.".encode()
_ANSWER = "The page reports a fixed fixture claim."
_IMAGE = "pgvector/pgvector:pg18-trixie"


@pytest.fixture(scope="module")
def workflow_pg_url() -> Iterator[str]:
    """Own one bounded cached container; never target an operator database."""
    pytest.importorskip("testcontainers")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
        from testcontainers.community.postgres import PostgresContainer

        postgres = PostgresContainer(
            _IMAGE,
            driver="asyncpg",
            username="scout",
            password="fixture",  # noqa: S106 - disposable database credential
            dbname="scout",
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=128,
        )
        # Require the selected local image before testcontainers can attempt a pull.
        postgres.get_docker_client().client.images.get(_IMAGE)
        # Docker accepts an address/port tuple; testcontainers' annotation is narrower.
        cast("Any", postgres.ports)["5432"] = ("127.0.0.1", None)
        with postgres:
            yield postgres.get_connection_url(host="127.0.0.1")


@pytest_asyncio.fixture
async def factory(workflow_pg_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(workflow_pg_url)
    tables = cast(
        "list[Table]",
        [
            Session.__table__,
            Run.__table__,
            Consent.__table__,
            RunDelivery.__table__,
            Step.__table__,
            ScoutBudget.__table__,
            ScoutAttempt.__table__,
        ],
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Run.metadata.drop_all, tables=tables)
            await connection.run_sync(Run.metadata.create_all, tables=tables)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Queue:
    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> Any:
        _ = job_or_func
        return SimpleNamespace(key=kwargs["key"])


class _LocalDispatcher(FakeDispatcher):
    @asynccontextmanager
    async def lease_grant(self, **kwargs: Any) -> AsyncGenerator[CapabilityGrant]:
        async with super().lease_grant(**kwargs) as grant:
            grant.spec.source_kind = SourceKind.SOULSTONE
            grant.spec.family = CapabilityFamily.CHAT
            yield grant


@dataclass
class _Assembly:
    access: LocalOperatorAccess
    authentication: LocalOperatorAuthentication
    profile: ScoutProfile
    authorizer: ScoutHostAuthorizer
    ledger: DbRunLedger
    consents: CodexConsentLedger
    store: DbScoutAttemptStore
    service: ScoutWorkflowService
    reviews: ScoutConsentReviews
    context: GraphRunContext[ScoutChatState, WorkflowServices]
    model_requests: list[bytes]
    turns: DbBridgeSessionStore


def _service(
    factory: async_sessionmaker[AsyncSession],
    profile: ScoutProfile,
    authorizer: ScoutHostAuthorizer,
    ledger: DbRunLedger,
    consents: CodexConsentLedger,
) -> tuple[DbScoutAttemptStore, ScoutWorkflowService]:
    completions = ScoutCompletions()
    store = DbScoutAttemptStore(factory, authorizer, profile.budget_limits, completions)
    return store, ScoutWorkflowService(
        store=store,
        completions=completions,
        ledger=ledger,
        consents=consents,
        profile=profile,
        capability_key=CAPABILITY,
        authorizer=authorizer,
        fence=RunEffectFence(),
    )


async def _assemble(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> _Assembly:
    """Keep real admission/claim owners around the inert execution adapters."""
    bwrap, python = tmp_path / "bwrap", tmp_path / "python"
    bwrap.touch()
    (python / "bin").mkdir(parents=True)
    (python / "bin/python3").touch()
    (python / "lib").mkdir()
    libraries = tuple(
        tmp_path / name
        for name in (
            "ld-linux-x86-64.so.2",
            "libc.so.6",
            "libdl.so.2",
            "libgcc_s.so.1",
            "libm.so.6",
            "libpthread.so.0",
            "librt.so.1",
            "libutil.so.1",
        )
    )
    for library in libraries:
        library.touch()
    profile = scout_profile().model_copy(update={"bwrap": bwrap, "python_root": python, "libraries": libraries})
    access = LocalOperatorAccess(access_password="fixture-secret")  # noqa: S106 - inert local test credential
    authentication = access.authenticate(username=b"magus", password=b"fixture-secret")
    assert authentication is not None
    authorizer = ScoutHostAuthorizer(
        access=access,
        policy=ScoutHostPolicy(
            policy_revision=profile.policy_revision,
            revocation_generation=profile.revocation_generation,
            actions=frozenset(ScoutAction),
            purposes=frozenset({SCOUT_ANSWER_PURPOSE}),
            categories=frozenset({"public-web-locator"}),
            binding_digests=frozenset({profile.binding_digest}),
            resolver_custody_digests=frozenset({profile.resolver_custody_digest}),
        ),
    )
    admission = ScoutAdmissionService(access=access, authorizer=authorizer, profile=profile)
    with pytest.raises(ScoutWorkflowError):
        admission.declare(prompt=_PROMPT, request_id=uuid4(), authentication=replace(authentication))
    proof = admission.declare(prompt=_PROMPT, request_id=uuid4(), authentication=authentication)
    ledger, consents = DbRunLedger(session_factory=factory), CodexConsentLedger(session_factory=factory)
    store, service = _service(factory, profile, authorizer, ledger, consents)
    turns = DbBridgeSessionStore(factory, sigil_name=default_local_sigil().name)
    session = await turns.create_session()
    bus = InProcessEventBus()
    engine = RunEngine(
        ledger=ledger,
        bus=bus,
        workflows=builtin_workflow_registry(
            weaver=WeaverSettings(
                bridge=BridgeCastingSettings(
                    revision="4",
                    capability_key=CAPABILITY,
                    scout=profile,
                )
            )
        ),
        queue_router=QueueRouter({"default": RouteRule(queue="runs", priority=50)}),
        queues={"runs": cast("Any", _Queue())},
        bridge_capability_key=CAPABILITY,
        bridge_scout=profile,
        scout_admission=admission,
        effects=service,
    )
    try:
        handle = await engine.submit(Intent(session_id=session.id, prompt=_PROMPT), scout_declaration=proof)
    finally:
        await bus.aclose()
    assert (await ledger.try_claim_run(handle.run_id, enqueue_seq=0)).acknowledged is True
    run = await ledger.get(handle.run_id)
    assert run is not None
    assert run.admitted_scout is not None
    assert run.admitted_scout.declaration == proof.declaration
    requests: list[bytes] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.function_tools
        requests.append(ModelMessagesTypeAdapter.dump_json(messages))
        return ModelResponse(parts=[TextPart(_ANSWER)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        await respond(messages, info)
        yield _ANSWER

    model = FunctionModel(respond, stream_function=stream)
    services = make_services(
        model=model,
        events=FakeEvents(),
        turns=FakeTurns(),
        consents=FakeConsents(),
        orchestrator=FakeOrchestrator(),
    )
    services = replace(
        services,
        scout=service,
        consents=consents,
        turns=turns,
        dispatcher=_LocalDispatcher(model=model, key=CAPABILITY),
    )
    state = cast("ScoutChatState", BRIDGE_CHAT_SCOUT.make_state(run.to_intent()))
    return _Assembly(
        access,
        authentication,
        profile,
        authorizer,
        ledger,
        consents,
        store,
        service,
        ScoutConsentReviews(access=access, authorizer=authorizer, store=store),
        GraphRunContext(state=state, deps=services),
        requests,
        turns,
    )


async def _approve(assembly: _Assembly) -> None:
    ctx = assembly.context
    assert isinstance(await PreparePageReview().run(ctx), AwaitPageConsent)
    assert ctx.state.review is not None
    review = ctx.state.review
    with pytest.raises(ConsentPending):
        await AwaitPageConsent().run(ctx)
    await assembly.ledger.park_consent(ctx.state.run_id, str(review.consent_id))
    view = await assembly.consents.get(str(review.consent_id))
    assert view is not None
    assert view.status == "pending"
    with pytest.raises(ConsentReviewForbiddenError):
        await assembly.reviews.resolve(
            consent=view,
            reviewer=default_local_sigil(),
            purpose="decide",
            authentication=replace(assembly.authentication),
        )
    resolution = await assembly.reviews.resolve(
        consent=view, reviewer=default_local_sigil(), purpose="decide", authentication=assembly.authentication
    )
    assert resolution.decision_allowed
    assert resolution.document is not None
    assert any(fact.value == _URL for fact in resolution.document.facts)
    granted = await assembly.consents.decide(
        str(review.consent_id),
        approved=True,
        decided_by=default_local_sigil().name,
        expected_native_binding=resolution.binding,
    )
    assert granted is not None
    assert granted.status == "granted"
    seq = await assembly.ledger.try_admit_consent(ctx.state.run_id, consent_id=str(review.consent_id))
    assert seq == 1
    assert (await assembly.ledger.try_claim_run(ctx.state.run_id, enqueue_seq=seq)).acknowledged is True
    assert isinstance(await AwaitPageConsent().run(ctx), AcquireAndAnswerPage)


def _acquisition(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], AsyncMock]:
    calls: list[str] = []

    async def fetch(candidate: DestinationCandidate, *, gate: NetworkGate, **kwargs: Any) -> FetchedPage:
        _ = kwargs
        calls.append(candidate.original_url)
        target = NetworkTarget("8.8.8.8", 443, candidate.hostname, "origin")
        gate.handoff(NetworkHandoff(target, "connect", b"", None), lambda: 0)
        wire = b"inert fixture ciphertext"
        gate.handoff(NetworkHandoff(target, "http", wire, serialize_get(candidate)), lambda: len(wire))
        observation = FetchObservation(
            candidate=candidate,
            addresses=(target.address,),
            peer=target.address,
            status=200,
            header_bytes=0,
            wire_body_bytes=len(_BODY),
            encoded_body_bytes=len(_BODY),
            decoded_body_bytes=len(_BODY),
            http_response_digest=sha256(_BODY).hexdigest(),
        )
        return FetchedPage(media_type="text/plain", body=_BODY, observations=(observation,))

    header = json.dumps({"media_type": "text/plain", "body_bytes": len(_BODY)}).encode() + b"\n"
    output = io.BytesIO()
    assert scout_worker.run(io.BytesIO(header + _BODY), output) == 0
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(output.getvalue())
    stdout.feed_eof()
    stderr.feed_eof()
    process = Mock(spec=asyncio.subprocess.Process)
    process.returncode, process.stdout, process.stderr = 0, stdout, stderr
    process.stdin = Mock(spec=asyncio.StreamWriter)
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock(return_value=0)
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(scout_workflow, "fetch_static_page", fetch)
    monkeypatch.setattr(scout_extraction.asyncio, "create_subprocess_exec", launch)
    return calls, launch


@pytest.mark.asyncio
async def test_retained_consent_to_transient_answer_and_lost_material_refusal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly = await _assemble(factory, tmp_path)
    calls, launch = _acquisition(monkeypatch)
    await _approve(assembly)
    assert calls == []
    ctx = assembly.context
    async with factory() as session:
        deliveries = list(
            (
                await session.scalars(
                    select(RunDelivery).where(RunDelivery.run_id == ctx.state.run_id).order_by(RunDelivery.enqueue_seq)
                )
            ).all()
        )
        assert [(row.enqueue_seq, row.state, row.resume) for row in deliveries] == [
            (0, "settled", False),
            (1, "claimed", True),
        ]
    assert isinstance(await AcquireAndAnswerPage().run(ctx), SettlePageAnswer)
    assert ctx.state.answer == _ANSWER
    assert _SOURCE not in ctx.state.model_dump_json()
    assert len(assembly.model_requests) == 1
    assert _SOURCE.encode() in assembly.model_requests[0]
    result = await SettlePageAnswer().run(ctx)
    assert result.data.answer == _ANSWER
    retained = await assembly.turns.get_session(ctx.state.session_id)
    assert retained is not None
    assert retained.turns[-1].content == _ANSWER
    assert _SOURCE not in json.dumps(retained.message_history)
    assert ctx.state.review is not None
    async with factory() as session:
        row = await session.get(ScoutAttempt, ctx.state.review.attempt_id)
        assert row is not None
        assert row.status == "succeeded"
        assert row.execution_enqueue_seq == 1
        settlement = ScoutSettlement.model_validate_json(row.settlement_bytes or b"")
        assert settlement.child == "reaped"
        assert settlement.usage.connects == 1
        assert _SOURCE.encode() not in settlement.handoff_evidence
        budgets = list((await session.scalars(select(ScoutBudget))).all())
        assert {row.scope for row in budgets} == {"host", "binding"}
        assert all(row.active_claims == 0 and row.connects_reserved == 0 and row.connects_used == 1 for row in budgets)
    _, restarted = _service(factory, assembly.profile, assembly.authorizer, assembly.ledger, assembly.consents)
    for owner in (assembly.service, restarted):
        with pytest.raises(ScoutWorkflowError) as error:
            await owner.acquire(run_id=ctx.state.run_id, binding=ctx.state.scout, review=ctx.state.review)
        assert error.value.code is ScoutWorkflowCode.MATERIAL_UNAVAILABLE
    assembly.access.revoke()
    with pytest.raises(FetchError):
        await restarted.authorize_settle(
            run_id=ctx.state.run_id, binding=ctx.state.scout, review=ctx.state.review, receipt_id=ctx.state.receipt_id
        )
    assert calls == [_URL]
    launch.assert_awaited_once()


@pytest.mark.asyncio
async def test_committed_claim_lost_acknowledgement_restarts_unknown_without_refetch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly = await _assemble(factory, tmp_path)
    calls, launch = _acquisition(monkeypatch)
    await _approve(assembly)
    ctx = assembly.context
    assert ctx.state.review is not None
    commit = scout_db._commit  # pyright: ignore[reportPrivateUsage] - inject loss after the actual commit

    async def lost_ack(session: AsyncSession) -> None:
        await commit(session)
        message = "Synthetic lost acknowledgement."
        raise ScoutCommitUnknownError(message)

    with monkeypatch.context() as patch:
        patch.setattr(scout_db, "_commit", lost_ack)
        with pytest.raises(ScoutCommitUnknownError):
            await assembly.service.acquire(run_id=ctx.state.run_id, binding=ctx.state.scout, review=ctx.state.review)
    _, restarted = _service(factory, assembly.profile, assembly.authorizer, assembly.ledger, assembly.consents)
    assert await restarted.reconcile_before(datetime.now(UTC) + timedelta(seconds=1)) == 1
    with pytest.raises(ScoutWorkflowError) as error:
        await restarted.acquire(run_id=ctx.state.run_id, binding=ctx.state.scout, review=ctx.state.review)
    assert error.value.code is ScoutWorkflowCode.MATERIAL_UNAVAILABLE
    async with factory() as session:
        row = await session.get(ScoutAttempt, ctx.state.review.attempt_id)
        assert row is not None
        assert row.status == "unknown_after_crash"
        assert row.receipt_id is None
        budgets = list((await session.scalars(select(ScoutBudget))).all())
        assert {row.scope for row in budgets} == {"host", "binding"}
        assert all(
            row.connects_reserved == assembly.profile.allowance.connects and row.connects_used == 0 for row in budgets
        )
        assert next(row for row in budgets if row.scope == "binding").active_claims == 1
    assert calls == []
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_real_extractor_preflight_refusal_settles_not_started(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly = await _assemble(factory, tmp_path)
    calls, launch = _acquisition(monkeypatch)
    await _approve(assembly)
    assembly.profile.bwrap.unlink()
    ctx = assembly.context
    assert ctx.state.review is not None
    with pytest.raises(ExtractionError) as error:
        await assembly.service.acquire(run_id=ctx.state.run_id, binding=ctx.state.scout, review=ctx.state.review)
    assert error.value.code is ExtractionCode.CONTAINMENT
    async with factory() as session:
        row = await session.get(ScoutAttempt, ctx.state.review.attempt_id)
        assert row is not None
        assert row.status == "refused"
        assert ScoutSettlement.model_validate_json(row.settlement_bytes or b"").child == "not_started"
        budgets = list((await session.scalars(select(ScoutBudget))).all())
        assert {row.scope for row in budgets} == {"host", "binding"}
        assert all(row.active_claims == 0 and row.connects_reserved == 0 for row in budgets)
    await assembly.service.contain(ctx.state.run_id)
    assert calls == [_URL]
    launch.assert_not_called()
