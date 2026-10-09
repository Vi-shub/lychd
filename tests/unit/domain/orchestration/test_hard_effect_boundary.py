"""Hard-transition restoration and its evidence survive cancellation and coalescing."""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from lychd.domain.cortex.leases import AnimatorAdmission
from lychd.domain.orchestration.actuator import TransitionIntent
from lychd.domain.orchestration.journal import TransitionJournal, TransitionRecord
from lychd.domain.orchestration.schema import TransitionTrace
from tests.capability_workflows import CapabilityScenario, build_capability_scenario


class _EvictingJournal(TransitionJournal):
    """Let unrelated evidence evict the owner before a cohort follower resumes."""

    def record(self, trace: TransitionTrace) -> TransitionRecord:
        record = super().record(trace)
        if trace.run_id == "owner-run" and trace.phase in {"completed", "failed_restored", "cancelled_restored"}:
            super().record(TransitionTrace(target_capability_key="unrelated:chat:model", priority=50))
            assert self.get(trace.request_id) is None
        return record


def _assert_admission(scenario: CapabilityScenario, *, closed: bool) -> None:
    assert scenario.broker.paused is closed
    expected = AnimatorAdmission.DRAINING if closed else AnimatorAdmission.OPEN
    assert all(scenario.leases.admission(name) is expected for name in ("a", "b"))


def _assert_shared_outcome(
    owner: TransitionTrace, follower: TransitionTrace, scenario: CapabilityScenario, expected_phase: str
) -> None:
    assert owner.phase == follower.phase == expected_phase
    assert owner.action_type == follower.action_type == "HARD_SWAP"
    assert owner.total_metabolic_cost == follower.total_metabolic_cost == 1.0
    assert owner.physical_transition_id == follower.physical_transition_id
    assert follower.physical_transition_id == scenario.actuator.intents[0].transition_id
    assert owner.compensation_transition_id == follower.compensation_transition_id
    if expected_phase != "completed":
        assert follower.compensation_transition_id == scenario.actuator.intents[1].transition_id


@pytest.mark.asyncio
@pytest.mark.parametrize("compensation_fails", [False, True], ids=["restored", "contained"])
async def test_observer_cancellation_cannot_skip_hard_compensation(
    monkeypatch: pytest.MonkeyPatch, *, compensation_fails: bool
) -> None:
    scenario = build_capability_scenario(active={"a"})
    compensation_entered, release_compensation = asyncio.Event(), asyncio.Event()
    projection_cancellation = asyncio.CancelledError("compensation observer cancelled")

    async def fail_warm(_key: str, *, timeout_s: float) -> None:
        _ = timeout_s
        message = "target readiness failed"
        raise RuntimeError(message)

    async def hold_compensation(intent: TransitionIntent) -> None:
        if intent.operation == "compensation":
            compensation_entered.set()
            await release_compensation.wait()
            if compensation_fails:
                message = "exact restoration failed"
                raise RuntimeError(message)

    def cancel_observation(record: TransitionRecord) -> None:
        if record.phase == "compensating":
            raise projection_cancellation

    monkeypatch.setattr(scenario.registry, "await_warm", fail_warm)
    scenario.actuator.before_apply = hold_compensation
    trace = TransitionTrace(target_capability_key="b:chat:b-model", priority=80, observer=cancel_observation)
    owner = asyncio.create_task(scenario.manager.request_transition(trace.target_capability_key, 80, trace=trace))
    try:
        await asyncio.wait_for(compensation_entered.wait(), timeout=1)
        _assert_admission(scenario, closed=True)
        assert scenario.world.active == {"b"}
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        release_compensation.set()
        expected_error = RuntimeError if compensation_fails else asyncio.CancelledError
        with pytest.raises(expected_error) as error:
            await owner
        if not compensation_fails:
            assert error.value is projection_cancellation
    finally:
        release_compensation.set()
        await asyncio.gather(owner, return_exceptions=True)

    assert [intent.operation for intent in scenario.actuator.intents] == ["forward", "compensation"]
    assert trace.compensation_transition_id == scenario.actuator.intents[1].transition_id
    assert trace.phase == ("contained_uncertain" if compensation_fails else "cancelled_restored")
    assert (scenario.manager.containment_reason is not None) is compensation_fails
    assert scenario.world.active == ({"b"} if compensation_fails else {"a"})
    _assert_admission(scenario, closed=compensation_fails)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_coalesced_hard_transition_keeps_terminal_evidence_after_journal_eviction(
    monkeypatch: pytest.MonkeyPatch, outcome: Literal["completed", "failed", "cancelled"]
) -> None:
    scenario = build_capability_scenario(active={"a"})
    scenario.manager.transitions = _EvictingJournal(capacity=1)
    warm_entered, release_warm, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    readiness_failure = RuntimeError("target readiness failed")
    original_await_warm = scenario.registry.await_warm

    async def hold_warm(key: str, *, timeout_s: float) -> object:
        warm_entered.set()
        await release_warm.wait()
        if outcome == "failed":
            raise readiness_failure
        return await original_await_warm(key, timeout_s=timeout_s)

    observations: list[TransitionRecord] = []

    def observe_follower(record: TransitionRecord) -> None:
        observations.append(record)
        if record.phase == "arbitrating":
            joined.set()

    monkeypatch.setattr(scenario.registry, "await_warm", hold_warm)
    key = "b:chat:b-model"
    owner_trace = TransitionTrace(target_capability_key=key, priority=80, run_id="owner-run")
    follower_trace = TransitionTrace(
        target_capability_key=key, priority=80, run_id="follower-run", observer=observe_follower
    )
    owner = asyncio.create_task(scenario.manager.request_transition(key, 80, trace=owner_trace))
    await asyncio.wait_for(warm_entered.wait(), timeout=1)
    follower = asyncio.create_task(scenario.manager.request_transition(key, 80, trace=follower_trace))
    try:
        await asyncio.wait_for(joined.wait(), timeout=1)
        if outcome == "cancelled":
            owner.cancel()
        else:
            release_warm.set()
        if outcome == "completed":
            await asyncio.gather(owner, follower)
        else:
            expected_error = asyncio.CancelledError if outcome == "cancelled" else RuntimeError
            for task in (owner, follower):
                with pytest.raises(expected_error) as error:
                    await task
                if outcome == "failed":
                    assert error.value is readiness_failure
    finally:
        release_warm.set()
        await asyncio.gather(owner, follower, return_exceptions=True)

    expected_phase = {"completed": "completed", "failed": "failed_restored", "cancelled": "cancelled_restored"}[outcome]
    _assert_shared_outcome(owner_trace, follower_trace, scenario, expected_phase)
    assert observations[-1].phase == expected_phase
    assert observations[-1].physical_transition_id == owner_trace.physical_transition_id
    assert observations[-1].compensation_transition_id == owner_trace.compensation_transition_id
    assert observations[-1].request_id == follower_trace.request_id != owner_trace.request_id
    assert observations[-1].run_id == "follower-run"
    assert scenario.manager.transitions.get(owner_trace.request_id) is None
    assert scenario.world.active == ({"b"} if outcome == "completed" else {"a"})
    _assert_admission(scenario, closed=False)


@pytest.mark.asyncio
async def test_cancelled_follower_does_not_inherit_the_owners_later_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = build_capability_scenario(active={"a"})
    entered, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_await_warm = scenario.registry.await_warm

    async def hold_warm(key: str, *, timeout_s: float) -> object:
        entered.set()
        await release.wait()
        return await original_await_warm(key, timeout_s=timeout_s)

    monkeypatch.setattr(scenario.registry, "await_warm", hold_warm)
    key = "b:chat:b-model"
    owner_trace = TransitionTrace(target_capability_key=key, priority=80)
    follower_trace = TransitionTrace(
        target_capability_key=key,
        priority=80,
        observer=lambda record: joined.set() if record.phase == "arbitrating" else None,
    )
    owner = asyncio.create_task(scenario.manager.request_transition(key, 80, trace=owner_trace))
    await asyncio.wait_for(entered.wait(), timeout=1)
    follower = asyncio.create_task(scenario.manager.request_transition(key, 80, trace=follower_trace))
    try:
        await asyncio.wait_for(joined.wait(), timeout=1)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        assert not owner.done()
        _assert_admission(scenario, closed=True)
        release.set()
        await owner
    finally:
        release.set()
        await asyncio.gather(owner, follower, return_exceptions=True)

    assert owner_trace.phase == "completed"
    assert follower_trace.phase == "failed"
    assert follower_trace.physical_transition_id is None
    assert len(scenario.actuator.intents) == 1
    _assert_admission(scenario, closed=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 3], ids=["first-cancel", "repeated-cancel"])
@pytest.mark.parametrize("compensation_fails", [False, True], ids=["restored", "contained"])
async def test_caller_cancellation_during_failure_compensation_is_retained(
    monkeypatch: pytest.MonkeyPatch, cancel_count: int, *, compensation_fails: bool
) -> None:
    """A new cancellation waits for the inverse and wins only after exact restoration."""
    scenario = build_capability_scenario(active={"a"})
    compensation_entered, release_compensation = asyncio.Event(), asyncio.Event()
    readiness_failure = RuntimeError("target readiness failed")
    compensation_failure = RuntimeError("exact restoration failed")

    async def fail_warm(_key: str, *, timeout_s: float) -> None:
        _ = timeout_s
        raise readiness_failure

    async def hold_compensation(intent: TransitionIntent) -> None:
        if intent.operation == "compensation":
            compensation_entered.set()
            await release_compensation.wait()
            if compensation_fails:
                raise compensation_failure

    monkeypatch.setattr(scenario.registry, "await_warm", fail_warm)
    scenario.actuator.before_apply = hold_compensation
    trace = TransitionTrace(target_capability_key="b:chat:b-model", priority=80)
    owner = asyncio.create_task(scenario.manager.request_transition(trace.target_capability_key, 80, trace=trace))
    try:
        await asyncio.wait_for(compensation_entered.wait(), timeout=1)
        for attempt in range(cancel_count):
            assert owner.cancel(f"caller cancellation {attempt}")
            await asyncio.sleep(0)
            assert not owner.done()
            assert trace.phase == "compensating"
            assert scenario.world.active == {"b"}
            _assert_admission(scenario, closed=True)
        release_compensation.set()
        if compensation_fails:
            with pytest.raises(RuntimeError, match="admission remains closed") as error:
                await owner
            assert error.value.__cause__ is compensation_failure
        else:
            with pytest.raises(asyncio.CancelledError, match="caller cancellation 0") as cancellation:
                await owner
            assert cancellation.value.__cause__ is readiness_failure
    finally:
        release_compensation.set()
        await asyncio.gather(owner, return_exceptions=True)

    assert [intent.operation for intent in scenario.actuator.intents] == ["forward", "compensation"]
    assert trace.compensation_transition_id == scenario.actuator.intents[1].transition_id
    expected_phase = "contained_uncertain" if compensation_fails else "cancelled_restored"
    assert trace.phase == expected_phase
    record = scenario.manager.transitions.get(trace.request_id)
    assert record is not None
    assert record.phase == expected_phase
    assert (scenario.manager.containment_reason is not None) is compensation_fails
    assert scenario.world.active == ({"b"} if compensation_fails else {"a"})
    _assert_admission(scenario, closed=compensation_fails)
