"""Real delegated_rite graph/worker behavior around acceptance-write uncertainty."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import pytest

from lychd.agents.router import Intent
from lychd.agents.workflows import DELEGATED_RITE
from lychd.domain.cortex.ledger import InMemoryRunLedger
from lychd.domain.cortex.runs import RunStatus
from lychd.domain.cortex.substrate import RunSubstrate
from lychd.domain.delegation import DelegatedAgentCoordinator, DelegatedAgentJob
from lychd.domain.delegation import DelegatedAgentJobStatus as Status
from lychd.domain.delegation.models import DelegatedAgentEventKind, DelegatedAgentResult
from lychd.ghouls.runs import perform_run
from tests.agents.fakes import FakeDispatcher
from tests.unit.domain.cortex.test_perform_run import _ProbeQueue, _substrate
from tests.unit.domain.delegation.test_acceptance_failure import AcceptanceRuntime, AcceptanceStore


class _ObservedStore(AcceptanceStore):
    def __init__(self, mode: str, timeline: list[str]) -> None:
        super().__init__(mode)
        self.timeline = timeline

    async def settle_acceptance_failure(
        self,
        job_id: str,
        result: DelegatedAgentResult,
    ) -> tuple[DelegatedAgentJob, bool]:
        outcome = await super().settle_acceptance_failure(job_id, result)
        self.timeline.append("acceptance disposition settled")
        return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["none", "before", "after"])
async def test_actual_delegated_graph_contains_uncertainty_or_completes_one_resume(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    timeline: list[str] = []

    store = _ObservedStore(mode, timeline)
    runtime = AcceptanceRuntime(timeline)
    coordinator = DelegatedAgentCoordinator(runtimes={runtime.name: runtime}, store=store)
    substrate, ledger, sessions = _substrate(dispatcher=FakeDispatcher(model=None))
    substrate.delegates = coordinator
    real_fail = ledger.try_fail_claimed

    async def observed_fail(*args: Any, **kwargs: Any) -> Any:
        timeline.append("Run failure entered")
        return await real_fail(*args, **kwargs)

    monkeypatch.setattr(ledger, "try_fail_claimed", observed_fail)
    session = await sessions.create_session(title="Acceptance-write contract")
    run_id = f"acceptance-{mode}"
    intent = Intent(session_id=session.id, run_id=run_id, prompt="/delegate bounded inert reply", source="bridge")
    await ledger.create(
        intent,
        workflow_name=DELEGATED_RITE.name,
        pattern_manifest=DELEGATED_RITE.manifest.snapshot(),
        queue_name="runs",
        priority=70,
    )

    if mode == "before":
        with pytest.raises(OSError, match="RUNNING write acknowledgement unavailable") as caught:
            await perform_run({"run_substrate": substrate}, run_id=run_id)
        assert caught.value is store.write_error
    else:
        first = await perform_run({"run_substrate": substrate}, run_id=run_id)
        assert first["status"] == "queued"
    run = await ledger.get(run_id)
    jobs = await store.jobs_for_run(run_id)
    assert run is not None
    assert len(jobs) == 1
    job = jobs[0]
    assert runtime.starts == [job.ref]
    assert store.running_calls == 1
    assert store.settlement_calls == (0 if mode == "none" else 1)
    if mode == "before":
        assert run.status is RunStatus.FAILED
        assert run.enqueue_seq == 0
        assert job.status is Status.CANCELLED
        assert runtime.cancels == [job.ref]
        assert [(e.kind, e.status) for e in job.events[-2:]] == [
            (DelegatedAgentEventKind.RESULT_ADOPTED, Status.LOST),
            (DelegatedAgentEventKind.STATUS_CHANGED, Status.CANCELLED),
        ]
        assert timeline.index("acceptance disposition settled") < timeline.index("containment returned")
        assert timeline.index("containment returned") < timeline.index("Run failure entered")
        assert not await substrate.stasis_store.exists(run_id)
    else:
        assert run.status is RunStatus.QUEUED
        assert run.enqueue_seq == 1
        assert await substrate.stasis_store.exists(run_id)
        assert runtime.cancels == []
        assert job.status is Status.SUCCEEDED
        await _assert_one_resume_finishes(substrate, ledger, store, run_id)


async def _assert_one_resume_finishes(
    substrate: RunSubstrate,
    ledger: InMemoryRunLedger,
    store: AcceptanceStore,
    run_id: str,
) -> None:
    fresh_runtime = AcceptanceRuntime()
    substrate.delegates = DelegatedAgentCoordinator(runtimes={fresh_runtime.name: fresh_runtime}, store=store)
    second = await perform_run({"run_substrate": substrate}, run_id=run_id, enqueue_seq=1)
    assert second["status"] == "done"
    done = await ledger.get(run_id)
    assert done is not None
    assert done.status is RunStatus.DONE
    assert done.enqueue_seq == 1
    assert not await substrate.stasis_store.exists(run_id)
    assert fresh_runtime.starts == fresh_runtime.cancels == []
    queue = substrate.queues["runs"]
    assert isinstance(queue, _ProbeQueue)
    assert len(queue.enqueued) == 1
