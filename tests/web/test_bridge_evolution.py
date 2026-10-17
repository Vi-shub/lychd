"""Offline HTTP, native admission, worker and retained comparison conversation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import uuid4

import httpx
import pytest

from lychd.agents.router import Intent
from lychd.agents.workflows import builtin_workflow_registry
from lychd.agents.workflows.evolution_compare import COMPARISON_SCOPE, comparison_reply
from lychd.config.settings import BridgeCastingSettings, WeaverSettings
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.events import InProcessEventBus, RunEventKind
from lychd.domain.cortex.ledger import InMemoryRunLedger, RunAdmissionConflictError
from lychd.domain.cortex.runs import RunStatus
from lychd.domain.cortex.substrate import RunSubstrate
from lychd.ghouls.runs import perform_run
from tests.agents.test_evolution_compare import comparison_prompt
from tests.web.conftest import TEST_AUTHORIZATION

if TYPE_CHECKING:
    from lychd.agents.factory import AgentForge
    from lychd.agents.services import GrantPort
    from lychd.domain.cortex.context import ContextOrchestrator
    from tests.web.conftest import AsgiClient


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    msg = "A deterministic comparison touched an effect port."
    raise AssertionError(msg)


class _ForbiddenPorts:
    def __getattr__(self, _name: str) -> NoReturn:
        return forbidden()


class _ClaimOnlyOrchestrator(_ForbiddenPorts):
    def __init__(self) -> None:
        gate = asyncio.Event()
        gate.set()
        self.worker_broker = SimpleNamespace(claim_gate=gate)


class _Queue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.jobs: dict[str, SimpleNamespace] = {}

    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"function": job_or_func, **kwargs})
        key = str(kwargs["key"])
        return self.jobs.setdefault(key, SimpleNamespace(key=key, status="queued"))

    async def job(self, job_key: str, /) -> SimpleNamespace | None:
        return self.jobs.get(job_key)

    async def abort(self, job: Any, error: str, /, ttl: float = 5) -> None:
        _ = job, error, ttl
        forbidden()


@pytest.fixture
def native(fake_services: SimpleNamespace) -> SimpleNamespace:
    ledger = InMemoryRunLedger()
    bus = InProcessEventBus(ledger=ledger)
    workflows = builtin_workflow_registry(weaver=WeaverSettings(bridge=BridgeCastingSettings(revision="5")))
    queue = _Queue()
    denied = _ForbiddenPorts()

    # Worker cleanup may release an empty Context; assembly remains forbidden.
    def release(_run_id: str) -> None:
        pass

    context = cast("ContextOrchestrator", SimpleNamespace(assemble=forbidden, release=release))
    substrate = RunSubstrate(
        ledger=ledger,
        bus=bus,
        workflows=workflows,
        orchestrator=_ClaimOnlyOrchestrator(),
        dispatcher=cast("GrantPort", denied),
        context=context,
        fragments=fake_services.fragments,
        turns=fake_services.bridge_sessions,
        consents=denied,
        forge=cast("AgentForge", denied),
        queues={"runs": queue},
    )
    engine = RunEngine(
        ledger=ledger,
        bus=bus,
        workflows=workflows,
        queue_router=QueueRouter({"default": RouteRule(queue="runs", priority=70)}),
        queues=substrate.queues,
        release_context=context.release,
    )
    fake_services.run_engine = engine
    fake_services.workflows = workflows
    fake_services.ledger = ledger
    fake_services.bus = bus
    return SimpleNamespace(engine=engine, substrate=substrate, queue=queue, ledger=ledger, bus=bus)


@pytest.mark.asyncio
async def test_http_to_worker_retains_exact_report_and_revision_across_retry_and_configuration_change(
    native: SimpleNamespace, altar_client: AsgiClient, fake_services: SimpleNamespace
) -> None:
    session = await fake_services.bridge_sessions.create_session()
    path = f"/api/v1/bridge/sessions/{session.id}/messages"
    request_id = str(uuid4())
    prompt = comparison_prompt()
    transport = httpx.ASGITransport(app=altar_client.app)  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        composer = await client.get(f"/api/v1/bridge/sessions/{session.id}")
        assert "/compare-history" in composer.json()["composer"]["guidance"]
        admitted = await client.post(path, json={"prompt": prompt, "request_id": request_id})
        assert admitted.status_code == 200
        run_id = admitted.json()["run_id"]
        assert admitted.json()["pattern_revision"] == "5"
        # New defaults cannot reroute queued work or its idempotent retry.
        legacy = builtin_workflow_registry()
        native.engine.workflows = legacy
        native.substrate.workflows = legacy
        result = await perform_run({"run_substrate": native.substrate}, run_id=run_id, enqueue_seq=0)
        assert result["status"] == "done"
        await native.bus.wait_persisted(run_id)
        replay = await client.post(path, json={"prompt": " \n" + prompt + "\t ", "request_id": request_id})
        assert replay.status_code == 200
        assert replay.json()["run_id"] == run_id
        changed = prompt.replace('"profile":', '"profile": ')
        conflict = await client.post(path, json={"prompt": changed, "request_id": request_id})
        assert conflict.status_code == 400
        refreshed = await client.get(f"/api/v1/bridge/sessions/{session.id}")
        turns = refreshed.json()["session"]["turns"]
        assert [(turn["role"], turn["run_id"]) for turn in turns] == [("user", run_id), ("agent", run_id)]
        assert turns[1]["content"] == comparison_reply(prompt).answer
        assert turns[1]["content"].startswith(COMPARISON_SCOPE)
        assert len(native.queue.calls) == 1
        retained = await fake_services.bridge_sessions.get_session(session.id)
        assert retained.message_history == []
        record = await native.ledger.get(run_id)
        assert record.status is RunStatus.DONE
        assert record.pattern_manifest["revision"] == "5"
        events = await native.ledger.list_events(run_id)
        assert not [
            event
            for event in events
            if event.kind in {RunEventKind.DISPATCH, RunEventKind.CONSENT, RunEventKind.TRANSITION}
        ]
        orb = await client.get(f"/api/v1/orb/runs/{run_id}")
        assert orb.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "conflict", "reject"])
async def test_blocked_and_rejected_comparisons_are_completed_calculations(
    native: SimpleNamespace, altar_client: AsgiClient, fake_services: SimpleNamespace, case: str
) -> None:
    prompt = comparison_prompt(result="unknown" if case == "unknown" else "pass", reject=case == "reject")
    if case == "conflict":
        command, body = prompt.split("\n", 1)
        packet = json.loads(body)
        summaries = packet["proposal"]["dispositions"][0]["evidence"]
        summaries.append({**summaries[0], "reported_result": "fail"})
        prompt = command + "\n" + json.dumps(packet)
    session = await fake_services.bridge_sessions.create_session()
    transport = httpx.ASGITransport(app=altar_client.app)  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        admitted = await client.post(
            f"/api/v1/bridge/sessions/{session.id}/messages", json={"prompt": prompt, "request_id": str(uuid4())}
        )
        assert admitted.status_code == 200
        run_id = admitted.json()["run_id"]
        assert (await perform_run({"run_substrate": native.substrate}, run_id=run_id, enqueue_seq=0))[
            "status"
        ] == "done"
        refreshed = await client.get(f"/api/v1/bridge/sessions/{session.id}")
        answer = refreshed.json()["session"]["turns"][-1]["content"]
        report = json.loads(answer.split("\n\n", 1)[1])
        assert report["outcome"] == ("rejection_recorded" if case == "reject" else "blocked")
        assert report["authority"] == "comparison-only"


@pytest.mark.asyncio
async def test_bad_input_refuses_before_run_retention_or_queue_publication(
    native: SimpleNamespace, altar_client: AsgiClient, fake_services: SimpleNamespace
) -> None:
    session = await fake_services.bridge_sessions.create_session()
    transport = httpx.ASGITransport(app=altar_client.app)  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        for prompt in (
            "wrong command",
            '/compare-history\n{"secret":1,"secret":2}',
            '/compare-history\n{"secret":NaN}',
            "x" * 100_001,
        ):
            response = await client.post(
                f"/api/v1/bridge/sessions/{session.id}/messages", json={"prompt": prompt, "request_id": str(uuid4())}
            )
            assert response.status_code == 400
            assert "secret" not in response.text
    with pytest.raises(RunAdmissionConflictError, match="bounded packet"):
        await native.engine.submit(Intent(session_id=session.id, prompt="x" * 100_001))
    assert native.queue.calls == []
    assert await native.ledger.get_nonterminal_for_session(session.id) is None
    assert (await fake_services.bridge_sessions.get_session(session.id)).turns == []
