"""Scout's explicit handler proof survives admission awaits and exact replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from lychd.agents.router import Intent
from lychd.agents.workflows import builtin_workflow_registry
from lychd.config.settings import BridgeCastingSettings, WeaverSettings
from lychd.domain.codex.local_access import LocalOperatorAccess, LocalOperatorAuthentication
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.events import InProcessEventBus
from lychd.domain.cortex.ledger import InMemoryRunLedger
from lychd.domain.cortex.runs import RunRecord, RunStatus
from lychd.domain.scout.profile import (
    ScoutBinding,
    ScoutDeclarationProof,
    ScoutProfile,
    ScoutWorkflowCode,
    ScoutWorkflowError,
)
from tests.scout_fixtures import CAPABILITY, PROMPT, scout_binding, scout_profile


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> Any:
        _ = job_or_func
        self.enqueued.append(kwargs["run_id"])
        return SimpleNamespace(key=kwargs["key"])

    async def job(self, job_key: str, /) -> Any:
        _ = job_key
        return None

    async def abort(self, job: Any, error: str, /, ttl: float = 5) -> None:
        _ = job, error, ttl


def _setup() -> tuple[RunEngine, InMemoryRunLedger, _Queue, LocalOperatorAccess, ScoutDeclarationProof]:
    access = LocalOperatorAccess(access_password="fixture-secret")  # noqa: S106 - inert test credential
    authentication = access.authenticate(username=b"magus", password=b"fixture-secret")
    assert authentication is not None
    binding = scout_binding()
    declaration = binding.declaration.model_copy(
        update={
            "subject": authentication.subject,
            "authentication_generation": authentication.authentication_generation,
        }
    )
    proof = ScoutDeclarationProof(
        authentication=authentication, declaration=declaration, question=binding.question, request_id=binding.request_id
    )

    def admit(intent: Intent, proof: ScoutDeclarationProof, profile: ScoutProfile) -> ScoutBinding:
        _ = intent
        if not access.current(proof.authentication):
            raise ScoutWorkflowError(ScoutWorkflowCode.ADMISSION)
        return ScoutBinding(
            profile=profile,
            declaration=proof.declaration,
            question=proof.question,
            request_id=proof.request_id,
        )

    ledger, queue = InMemoryRunLedger(), _Queue()
    settings = WeaverSettings(
        bridge=BridgeCastingSettings(revision="4", capability_key=CAPABILITY, scout=scout_profile())
    )
    engine = RunEngine(
        ledger=ledger,
        bus=InProcessEventBus(ledger=ledger),
        workflows=builtin_workflow_registry(weaver=settings),
        queue_router=QueueRouter({"default": RouteRule(queue="runs", priority=50)}),
        queues={"runs": queue},
        bridge_capability_key=CAPABILITY,
        bridge_scout=scout_profile(),
        scout_admission=admit,
    )
    return engine, ledger, queue, access, proof


@pytest.mark.asyncio
async def test_plain_intent_or_reconstructed_auth_marker_cannot_admit_scout() -> None:
    engine, _, queue, _, proof = _setup()
    intent = Intent(session_id="session", prompt=PROMPT)
    with pytest.raises(ScoutWorkflowError):
        await engine.submit(intent)
    forged = LocalOperatorAuthentication(
        subject=proof.authentication.subject, authentication_generation=proof.authentication.authentication_generation
    )
    with pytest.raises(ScoutWorkflowError):
        await engine.submit(intent, scout_declaration=replace(proof, authentication=forged))
    with pytest.raises(ValueError, match="server-owned"):
        await engine.submit(intent.model_copy(update={"admitted_scout": scout_binding()}), scout_declaration=proof)
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_fresh_timestamp_retry_preserves_original_declared_time_and_refuses_changed_binding() -> None:
    engine, ledger, queue, _, proof = _setup()
    intent = Intent(session_id="session", prompt=PROMPT)
    first = await engine.submit(intent, scout_declaration=proof, idempotency_key="scout-request")
    run = await ledger.get(first.run_id)
    assert run is not None
    assert run.admitted_scout is not None
    assert run.to_intent().admitted_scout == run.admitted_scout
    assert run.pattern_manifest["revision"] == "4"
    later = replace(
        proof,
        declaration=proof.declaration.model_copy(
            update={"declared_at": proof.declaration.declared_at + timedelta(seconds=10)}
        ),
    )
    replay = await engine.submit(intent, scout_declaration=later, idempotency_key="scout-request")
    assert replay.run_id == first.run_id
    assert run.admitted_scout.declaration.declared_at == proof.declaration.declared_at
    with pytest.raises(ScoutWorkflowError):
        await engine.submit(
            intent, scout_declaration=replace(proof, question="changed question"), idempotency_key="scout-request"
        )
    engine.bridge_capability_key = "different:chat:model"
    with pytest.raises(ScoutWorkflowError):
        await engine.submit(intent, scout_declaration=proof, idempotency_key="scout-request")
    assert queue.enqueued == [first.run_id]


@pytest.mark.asyncio
async def test_revocation_during_retention_fails_held_admission_before_publication() -> None:
    engine, ledger, queue, access, proof = _setup()
    retained: list[str] = []

    async def retain(run_id: str) -> None:
        retained.append(run_id)
        access.revoke()

    with pytest.raises(ScoutWorkflowError):
        await engine.submit(
            Intent(session_id="session", prompt=PROMPT), scout_declaration=proof, retain_before_publish=retain
        )
    assert len(retained) == 1
    run = await ledger.get(retained[0])
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert queue.enqueued == []


@pytest.mark.asyncio
async def test_revocation_during_commit_refuses_before_turn_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, ledger, queue, access, proof = _setup()
    original = ledger.create
    created: list[RunRecord] = []
    retained: list[str] = []

    async def create(*args: Any, **kwargs: Any) -> RunRecord:
        run = await original(*args, **kwargs)
        created.append(run)
        access.revoke()
        return run

    async def retain(run_id: str) -> None:
        retained.append(run_id)

    monkeypatch.setattr(ledger, "create", create)
    with pytest.raises(ScoutWorkflowError):
        await engine.submit(
            Intent(session_id="session", prompt=PROMPT), scout_declaration=proof, retain_before_publish=retain
        )
    assert len(created) == 1
    retained_run = await ledger.get(created[0].run_id)
    assert retained_run is not None
    assert retained_run.status is RunStatus.FAILED
    assert retained == queue.enqueued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "ordinary question",
        "/read-public https://example.com/",
        "/read-public https://example.com/\n  ",
        "/read-public https://example.com/ extra\nQuestion",
        "/read-public http://example.com/\nQuestion",
    ],
)
async def test_malformed_command_refuses_before_turn_retention(prompt: str) -> None:
    engine, _, queue, _, proof = _setup()
    retained: list[str] = []

    async def retain(run_id: str) -> None:
        retained.append(run_id)

    with pytest.raises(ScoutWorkflowError):
        await engine.submit(
            Intent(session_id="session", prompt=prompt), scout_declaration=proof, retain_before_publish=retain
        )
    assert retained == queue.enqueued == []
