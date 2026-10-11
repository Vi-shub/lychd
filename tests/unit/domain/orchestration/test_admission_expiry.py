"""Admission clocks surround real coordination and inert runtime effects."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from lychd.config.settings.orchestration import SwitchingSettings
from lychd.domain.animation.capabilities import CapabilityPhase
from lychd.domain.animation.errors import ActivationTimeout
from lychd.domain.animation.services import registry as registry_module
from lychd.domain.cortex.leases import AnimatorAdmission
from lychd.domain.orchestration.actuator import RuntimeCancellationNoEffectError, RuntimePreconditionError
from lychd.domain.orchestration.arbiter import TransitionAdmissionExpired, TransitionAdmissionWaitExpired
from lychd.domain.orchestration.journal import TransitionRecord
from lychd.domain.orchestration.schema import TransitionTrace
from lychd.system.services.runtime_guard import FileSoftActivationGuard
from tests.capability_workflows import CapabilityScenario, build_capability_scenario

if TYPE_CHECKING:
    from pathlib import Path

    from lychd.domain.orchestration.containment import SoftActivationRecord


@dataclass
class _Clock:
    now: float

    def time(self) -> float:
        return self.now


def _clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    loop = asyncio.get_running_loop()
    clock = _Clock(loop.time())
    monkeypatch.setattr(loop, "time", clock.time)
    monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=clock.time))
    return clock


def _scenario(*, soft: bool = False) -> CapabilityScenario:
    scenario = (
        build_capability_scenario(models={"router": ("small", "large")}, active={"router"})
        if soft
        else build_capability_scenario(active={"a"})
    )
    scenario.manager._switching = SwitchingSettings(planning_timeout_s=100, drain_timeout_s=100, warmup_timeout_s=100)
    return scenario


async def _turns() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -float("inf"), True, "bad"])
async def test_invalid_absolute_deadline_precedes_trace_cohort_and_probe(deadline: Any) -> None:
    scenario = _scenario()
    with pytest.raises(ValueError, match="finite"):
        await scenario.manager.request_transition("b:chat:b-model", 50, admission_deadline=deadline)
    assert scenario.manager.transitions.recent() == ()
    assert scenario.manager._arbiter._inflight == {}
    assert scenario.world.probes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["a:chat:a-model", "b:chat:b-model"])
async def test_expired_entry_does_not_probe_even_a_warm_target(target: str) -> None:
    scenario = _scenario()
    with pytest.raises(TransitionAdmissionExpired):
        await scenario.manager.request_transition(target, 50, admission_deadline=asyncio.get_running_loop().time())
    assert scenario.manager.transitions.recent() == ()
    assert scenario.world.probes == []
    assert scenario.actuator.intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("soft", [False, True], ids=["hard", "soft"])
async def test_final_guard_follows_synchronous_actuating_observer(
    monkeypatch: pytest.MonkeyPatch, *, soft: bool
) -> None:
    scenario = _scenario(soft=soft)
    clock = _clock(monkeypatch)
    cutoff = clock.now + 10

    def observe(record: TransitionRecord) -> None:
        if record.phase == "actuating":
            clock.now = cutoff

    target = "router:chat:large" if soft else "b:chat:b-model"
    trace = TransitionTrace(target_capability_key=target, priority=50, observer=observe)
    with pytest.raises(TransitionAdmissionExpired):
        await scenario.manager.request_transition(target, 50, trace=trace, admission_deadline=cutoff)
    assert scenario.actuator.intents == []
    assert scenario.world.activations == []
    assert scenario.manager.containment_reason is None
    assert not scenario.broker.paused
    assert trace.phase == "declined_no_effect"


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_budget", [5.0, 10.0, 20.0, None])
async def test_durable_soft_guard_cannot_retire_or_overrun_invocation_cutoffs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner_budget: float | None
) -> None:
    directory = tmp_path / "runtime-guard"
    directory.mkdir(mode=0o700)
    guard = FileSoftActivationGuard(directory)
    scenario = build_capability_scenario(
        models={"router": ("small", "large")}, active={"router"}, soft_activation_guard=guard
    )
    scenario.manager._switching = SwitchingSettings(planning_timeout_s=100, drain_timeout_s=100, warmup_timeout_s=10)
    clock = _clock(monkeypatch)
    start = clock.now
    original_arm = guard.arm

    def delayed_arm(record: SoftActivationRecord) -> None:
        original_arm(record)
        # Real write-ahead custody exists, but synchronous fsync may consume the
        # remaining budget before the event loop can deliver its timer callback.
        clock.now = start + 10

    monkeypatch.setattr(guard, "arm", delayed_arm)
    error = TransitionAdmissionExpired if owner_budget == 5 else ActivationTimeout
    with pytest.raises(error):
        await scenario.manager.request_transition(
            "router:chat:large", 50, admission_deadline=None if owner_budget is None else start + owner_budget
        )
    assert scenario.world.activations == []
    assert scenario.world.loaded["router"] == "small"
    assert scenario.actuator.intents == []
    assert scenario.manager.containment_reason is not None
    assert scenario.broker.paused
    assert scenario.leases.admission("router") is AnimatorAdmission.DRAINING
    assert scenario.leases.active() == []
    pending = guard.pending()
    assert pending is not None
    assert pending.target_capability_key == "router:chat:large"
    fresh_guard = FileSoftActivationGuard(directory)
    assert fresh_guard.pending() == pending
    with pytest.raises(RuntimePreconditionError, match="recovery"):
        fresh_guard.assert_clear()
    with pytest.raises(RuntimeError, match="containment is active"):
        await scenario.manager.request_transition("router:chat:large", 50)
    assert fresh_guard.pending() == pending


@pytest.mark.asyncio
async def test_follower_cutoff_uses_invocation_after_durable_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(soft=True)
    clock = _clock(monkeypatch)
    start = clock.now
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(_name: str) -> None:
        entered.set()
        await release.wait()

    guard = scenario.manager._soft_activation_guard
    original_arm = guard.arm

    def delayed_arm(record: SoftActivationRecord) -> None:
        original_arm(record)
        clock.now = start + 6

    monkeypatch.setattr(guard, "arm", delayed_arm)
    scenario.world.before_probe = block
    target = "router:chat:large"
    owner = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=start + 20))
    await entered.wait()
    follower = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=start + 5))
    try:
        await _turns()
        release.set()
        results = await asyncio.gather(owner, follower, return_exceptions=True)
        assert not isinstance(results[0], BaseException)
        assert type(results[1]) is TransitionAdmissionWaitExpired
        assert scenario.world.activations == [target]
        assert scenario.manager.containment_reason is None
        assert not scenario.broker.paused
        assert scenario.leases.admission("router") is AnimatorAdmission.OPEN
        guard.assert_clear()
    finally:
        release.set()
        for task in (owner, follower):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("soft", [False, True], ids=["hard", "soft"])
async def test_timely_invocation_retires_admission_before_late_settlement(
    monkeypatch: pytest.MonkeyPatch, *, soft: bool
) -> None:
    scenario = _scenario(soft=soft)
    clock = _clock(monkeypatch)
    cutoff = clock.now + 10

    def observe(record: TransitionRecord) -> None:
        if record.phase == "completed":
            clock.now = cutoff + 1

    async def late_apply(_intent: Any) -> None:
        clock.now = cutoff + 1

    original_activate = scenario.world.activate_capability

    async def late_activate(animator: Any, spec: Any) -> Any:
        result = await original_activate(animator, spec)
        clock.now = cutoff + 1
        return result

    scenario.actuator.before_apply = late_apply
    monkeypatch.setattr(scenario.world, "activate_capability", late_activate)
    target = "router:chat:large" if soft else "b:chat:b-model"
    trace = TransitionTrace(target_capability_key=target, priority=50, observer=observe)
    plan = await scenario.manager.request_transition(target, 50, trace=trace, admission_deadline=cutoff)
    assert plan.action_type == ("SOFT_SWAP" if soft else "HARD_SWAP")
    assert trace.phase == "completed"
    assert not scenario.broker.paused


@pytest.mark.asyncio
async def test_timely_noop_confirmation_survives_late_completion_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)
    cutoff = clock.now + 1

    def observe(record: TransitionRecord) -> None:
        if record.phase == "completed":
            clock.now = cutoff

    trace = TransitionTrace(target_capability_key="a:chat:a-model", priority=50, observer=observe)
    plan = await scenario.manager.request_transition(
        trace.target_capability_key, 50, trace=trace, admission_deadline=cutoff
    )
    assert plan.action_type == "NO_OP"
    assert scenario.actuator.intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("planning_pass", [1, 2])
@pytest.mark.parametrize("owner_budget", [None, 5.0, 10.0, 20.0])
async def test_original_planning_cutoff_wins_ties_and_rejects_delayed_success(
    monkeypatch: pytest.MonkeyPatch, planning_pass: int, owner_budget: float | None
) -> None:
    scenario = _scenario()
    scenario.manager._switching.planning_timeout_s = 10
    clock = _clock(monkeypatch)
    cutoff = None if owner_budget is None else clock.now + owner_budget
    passes = 0

    async def delay(name: str) -> None:
        nonlocal passes
        if name == "a":
            passes += 1
            if passes == planning_pass:
                clock.now += 11

    scenario.world.before_probe = delay
    error = TransitionAdmissionExpired if owner_budget == 5 else RuntimePreconditionError
    with pytest.raises(error):
        await scenario.manager.request_transition("b:chat:b-model", 50, admission_deadline=cutoff)
    assert scenario.actuator.intents == []
    assert not scenario.broker.paused


@pytest.mark.asyncio
async def test_preview_also_rejects_success_after_its_planning_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)

    async def delay(_name: str) -> None:
        clock.now += 101

    scenario.world.before_probe = delay
    with pytest.raises(RuntimePreconditionError, match="planning"):
        await scenario.manager.calculate_transition_plan("b:chat:b-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_timed", [False, True])
async def test_follower_expiry_leaves_owner_and_other_follower_running(
    monkeypatch: pytest.MonkeyPatch, *, owner_timed: bool
) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(_name: str) -> None:
        entered.set()
        await release.wait()

    scenario.world.before_probe = block
    target = "b:chat:b-model"
    owner = asyncio.create_task(
        scenario.manager.request_transition(target, 50, admission_deadline=clock.now + 20 if owner_timed else None)
    )
    await entered.wait()
    follower = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=clock.now + 5))
    survivor = asyncio.create_task(scenario.manager.request_transition(target, 50))
    try:
        await _turns()
        clock.now += 6
        await _turns()
        with pytest.raises(TransitionAdmissionWaitExpired):
            await follower
        assert not owner.done()
        release.set()
        a, b = await asyncio.gather(owner, survivor)
        assert a is b
        assert len(scenario.actuator.intents) == 1
    finally:
        release.set()
        for task in (owner, follower, survivor):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, survivor, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("follower_budget", [None, 5.0, 20.0])
async def test_owner_expiry_is_shared_without_transferring_ownership(
    monkeypatch: pytest.MonkeyPatch, follower_budget: float | None
) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)
    entered = asyncio.Event()

    async def block(_name: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    scenario.world.before_probe = block
    target = "b:chat:b-model"
    owner = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=clock.now + 5))
    await entered.wait()
    follower = asyncio.create_task(
        scenario.manager.request_transition(
            target, 50, admission_deadline=None if follower_budget is None else clock.now + follower_budget
        )
    )
    await _turns()
    clock.now += 6
    results = await asyncio.gather(owner, follower, return_exceptions=True)
    assert all(type(result) is TransitionAdmissionExpired for result in results)
    assert results[0] is results[1]
    assert scenario.actuator.intents == []
    assert scenario.manager._arbiter._inflight == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [4.0, 5.0, 6.0])
async def test_follower_uses_actual_boundary_even_before_its_timer_runs(
    monkeypatch: pytest.MonkeyPatch, handoff: float
) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)
    start = clock.now
    entered, release = asyncio.Event(), asyncio.Event()

    async def block(_name: str) -> None:
        entered.set()
        await release.wait()

    async def settle_late(_intent: Any) -> None:
        clock.now = start + 8

    def observe(record: TransitionRecord) -> None:
        if record.phase == "actuating":
            clock.now = start + handoff

    scenario.world.before_probe = block
    scenario.actuator.before_apply = settle_late
    trace = TransitionTrace(target_capability_key="b:chat:b-model", priority=50, observer=observe)
    owner = asyncio.create_task(scenario.manager.request_transition(trace.target_capability_key, 50, trace=trace))
    await entered.wait()
    follower = asyncio.create_task(
        scenario.manager.request_transition(trace.target_capability_key, 50, admission_deadline=start + 5)
    )
    await _turns()
    release.set()
    outcomes = await asyncio.gather(owner, follower, return_exceptions=True)
    assert not isinstance(outcomes[0], BaseException)
    if handoff < 5:
        assert outcomes[0] is outcomes[1]
    else:
        assert type(outcomes[1]) is TransitionAdmissionWaitExpired
    assert len(scenario.actuator.intents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_budget", [5.0, 10.0, 20.0, None])
async def test_drain_deadline_starts_after_pause_and_preserves_real_grant(
    monkeypatch: pytest.MonkeyPatch, owner_budget: float | None
) -> None:
    scenario = _scenario()
    scenario.manager._switching.drain_timeout_s = 6
    clock = _clock(monkeypatch)
    start = clock.now
    original_pause = scenario.broker.pause_queues
    drain_started = asyncio.Event()

    async def pause() -> None:
        await original_pause()
        clock.now += 4

    original_drain = scenario.leases.drained

    async def drain(*args: Any, **kwargs: Any) -> bool:
        drain_started.set()
        return await original_drain(*args, **kwargs)

    monkeypatch.setattr(scenario.broker, "pause_queues", pause)
    monkeypatch.setattr(scenario.leases, "drained", drain)
    async with scenario.dispatcher.lease_grant(family="chat", model_name="a-model", run_id="retained") as grant:
        owner = asyncio.create_task(
            scenario.manager.request_transition(
                "b:chat:b-model", 50, admission_deadline=None if owner_budget is None else start + owner_budget
            )
        )
        await drain_started.wait()
        clock.now = start + 11
        error = TransitionAdmissionExpired if owner_budget == 5 else RuntimeError
        with pytest.raises(error) as caught:
            await owner
        if owner_budget != 5:
            assert "Lease drain timed out" in str(caught.value)
        assert scenario.leases.active()[0].grant_id == grant.lease.grant_id
        assert scenario.leases.admission("a") is AnimatorAdmission.OPEN
        assert not scenario.broker.paused
        assert scenario.actuator.intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("soft", [False, True])
async def test_post_admission_late_warmth_retains_compensation_or_containment(
    monkeypatch: pytest.MonkeyPatch, *, soft: bool
) -> None:
    scenario = _scenario(soft=soft)
    scenario.manager._switching.warmup_timeout_s = 10
    clock = _clock(monkeypatch)
    start = clock.now
    original_probe = scenario.world.probe_capability_states
    late = False

    async def probe(animator: Any, specs: Any) -> Any:
        nonlocal late
        states = await original_probe(animator, specs)
        if not late and (scenario.world.activations or scenario.actuator.intents):
            late = True
            clock.now = start + 11
        return states

    monkeypatch.setattr(scenario.world, "probe_capability_states", probe)
    target = "router:chat:large" if soft else "b:chat:b-model"
    with pytest.raises(ActivationTimeout):
        await scenario.manager.request_transition(target, 50, admission_deadline=start + 5)
    if soft:
        assert scenario.manager.containment_reason is not None
        assert scenario.broker.paused
    else:
        assert [intent.operation for intent in scenario.actuator.intents] == ["forward", "compensation"]
        assert not scenario.broker.paused


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_budget", [5.0, 10.0, 20.0, None])
async def test_observation_only_soft_budget_precedence_reopens_gates(
    monkeypatch: pytest.MonkeyPatch, owner_budget: float | None
) -> None:
    scenario = _scenario(soft=True)
    scenario.manager._switching.warmup_timeout_s = 10
    clock = _clock(monkeypatch)
    start = clock.now
    original_probe = scenario.world.probe_capability_states

    async def probe(animator: Any, specs: Any) -> Any:
        states = await original_probe(animator, specs)
        if scenario.broker.paused:
            clock.now = start + 11
        return [state.model_copy(update={"phase": CapabilityPhase.WARMING}) for state in states]

    monkeypatch.setattr(scenario.world, "probe_capability_states", probe)
    error = TransitionAdmissionExpired if owner_budget == 5 else RuntimePreconditionError
    with pytest.raises(error) as caught:
        await scenario.manager.request_transition(
            "router:chat:large", 50, admission_deadline=None if owner_budget is None else start + owner_budget
        )
    if owner_budget != 5:
        assert isinstance(caught.value.__cause__, ActivationTimeout)
    assert scenario.world.activations == []
    assert scenario.manager.containment_reason is None
    assert not scenario.broker.paused


def _hold_soft_observation_cleanup(
    scenario: CapabilityScenario,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
) -> tuple[asyncio.Event, asyncio.Event, asyncio.Event]:
    polling, cleaning, release_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_probe = scenario.world.probe_capability_states
    closed_probes = 0

    async def probe(animator: Any, specs: Any) -> Any:
        nonlocal closed_probes
        states = await original_probe(animator, specs)
        if scenario.broker.paused:
            closed_probes += 1
            if closed_probes > 1:
                polling.set()
                if fail:
                    message = "original inventory error"
                    raise OSError(message)
                await asyncio.Event().wait()
        return [state.model_copy(update={"phase": CapabilityPhase.WARMING}) for state in states]

    async def abandon(_animator: Any, _spec: Any) -> None:
        cleaning.set()
        await release_cleanup.wait()

    monkeypatch.setattr(scenario.world, "probe_capability_states", probe)
    monkeypatch.setattr(scenario.world, "abandon_activation", abandon, raising=False)
    return polling, cleaning, release_cleanup


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase_budget", "owner_budget"), [(10, 20), (20, 10), (10, 10)])
@pytest.mark.parametrize("follower_budget", [9, 10, 12])
async def test_frozen_deadline_precedence_survives_cleanup_and_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch, phase_budget: int, owner_budget: int, follower_budget: int
) -> None:
    scenario = _scenario(soft=True)
    scenario.manager._switching.warmup_timeout_s = phase_budget
    clock = _clock(monkeypatch)
    start = clock.now
    polling, cleaning, release = _hold_soft_observation_cleanup(scenario, monkeypatch)
    target = "router:chat:large"
    owner = asyncio.create_task(
        scenario.manager.request_transition(target, 50, admission_deadline=start + owner_budget)
    )
    await polling.wait()
    follower = asyncio.create_task(
        scenario.manager.request_transition(target, 50, admission_deadline=start + follower_budget)
    )
    try:
        await _turns()
        clock.now = start + 10
        await cleaning.wait()
        owner.cancel()
        await _turns()
        owner.cancel()
        clock.now = start + 14
        await _turns()
        if follower_budget < 10:
            with pytest.raises(TransitionAdmissionWaitExpired):
                await follower
        else:
            assert not follower.done()
        assert scenario.broker.paused
        release.set()
        outcomes = await asyncio.gather(owner, follower, return_exceptions=True)
        if phase_budget <= owner_budget:
            assert isinstance(outcomes[0], RuntimePreconditionError)
            assert isinstance(outcomes[0].__cause__, ActivationTimeout)
        else:
            assert type(outcomes[0]) is TransitionAdmissionExpired
        if follower_budget >= 10:
            assert outcomes[1] is outcomes[0]
        assert not scenario.broker.paused
        assert scenario.world.activations == []
    finally:
        release.set()
        for task in (owner, follower):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_cancel", [False, True], ids=["ordinary-error", "explicit-cancellation"])
async def test_failure_starting_cleanup_is_not_replaced_by_later_expiry(
    monkeypatch: pytest.MonkeyPatch, *, explicit_cancel: bool
) -> None:
    scenario = _scenario(soft=True)
    scenario.manager._switching.warmup_timeout_s = 2
    clock = _clock(monkeypatch)
    cutoff = clock.now + 1
    polling, cleaning, release = _hold_soft_observation_cleanup(scenario, monkeypatch, fail=not explicit_cancel)
    target = "router:chat:large"
    owner = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=cutoff))
    await polling.wait()
    if explicit_cancel:
        owner.cancel()
    await cleaning.wait()
    follower = asyncio.create_task(scenario.manager.request_transition(target, 50))
    try:
        # Both original budgets pass while the earlier ordinary failure or
        # explicit cancellation is already completing adapter abandonment.
        clock.now = cutoff + 2
        await _turns()
        release.set()
        expected = RuntimeCancellationNoEffectError if explicit_cancel else RuntimePreconditionError
        causes: list[BaseException] = []
        for task in (owner, follower):
            with pytest.raises(expected) as error:
                await task
            causes.append(error.value)
            assert isinstance(error.value.__cause__, asyncio.CancelledError if explicit_cancel else OSError)
        assert causes[0] is causes[1]
        assert not scenario.broker.paused
        assert scenario.manager.containment_reason is None
    finally:
        release.set()
        for task in (owner, follower):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_expiry_withdraws_cohort_and_preserves_predecessor_retry_and_third_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = build_capability_scenario(
        models={name: (f"{name}-model",) for name in ("a", "b", "c", "d")}, active={"a"}
    )
    scenario.manager._switching = SwitchingSettings(planning_timeout_s=100, drain_timeout_s=100, warmup_timeout_s=100)
    clock = _clock(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_first(_intent: Any) -> None:
        if len(scenario.actuator.intents) == 1:
            entered.set()
            await release.wait()

    scenario.actuator.before_apply = hold_first
    first = asyncio.create_task(scenario.manager.request_transition("b:chat:b-model", 50))
    await entered.wait()
    expiring = asyncio.create_task(
        scenario.manager.request_transition("c:chat:c-model", 50, admission_deadline=clock.now + 5)
    )
    third = asyncio.create_task(scenario.manager.request_transition("d:chat:d-model", 50))
    try:
        await _turns()
        clock.now += 6
        with pytest.raises(TransitionAdmissionExpired):
            await expiring
        assert not first.done()
        assert len(scenario.actuator.intents) == 1
        release.set()
        await asyncio.gather(first, third)
        await scenario.manager.request_transition("c:chat:c-model", 50)
        assert [intent.target_animator for intent in scenario.actuator.intents] == ["b", "d", "c"]
    finally:
        release.set()
        for task in (first, expiring, third):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, expiring, third, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_a_planning_probe_finalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _scenario()
    clock = _clock(monkeypatch)
    probing, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def probe(_name: str) -> None:
        probing.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    scenario.world.before_probe = probe
    target = "b:chat:b-model"
    owner = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=clock.now + 5))
    await probing.wait()
    follower = asyncio.create_task(scenario.manager.request_transition(target, 50))
    try:
        clock.now += 5
        await cleaning.wait()
        owner.cancel()
        await _turns()
        owner.cancel()
        await _turns()
        assert not cleaned.is_set()
        assert not owner.done()
        assert not follower.done()
        assert scenario.registry._probe_lock.locked()
        release.set()
        outcomes = await asyncio.gather(owner, follower, return_exceptions=True)
        assert cleaned.is_set()
        assert all(type(outcome) is TransitionAdmissionExpired for outcome in outcomes)
        assert outcomes[0] is outcomes[1]
        assert not scenario.registry._probe_lock.locked()
        assert scenario.actuator.intents == []
    finally:
        release.set()
        for task in (owner, follower):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("follower_budget", [0.5, 1.0, 2.0, None])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_selected_admission_expiry_survives_simultaneously_overdue_registry_timeout(
    monkeypatch: pytest.MonkeyPatch, follower_budget: float | None, *, repeat_cancel: bool
) -> None:
    scenario = _scenario(soft=True)
    clock = _clock(monkeypatch)
    start = clock.now
    polling, cleaning, release = _hold_soft_observation_cleanup(scenario, monkeypatch)
    target = "router:chat:large"
    owner = asyncio.create_task(scenario.manager.request_transition(target, 50, admission_deadline=start + 1))
    await polling.wait()
    follower = asyncio.create_task(
        scenario.manager.request_transition(
            target, 50, admission_deadline=None if follower_budget is None else start + follower_budget
        )
    )
    try:
        await _turns()
        # The admission timer and registry's original 100-second warm-up timer
        # are both due. The registry may translate cancellation to its own typed
        # timeout before its held abandonment finishes.
        clock.now = start + 101
        await cleaning.wait()
        if repeat_cancel:
            owner.cancel()
            await _turns()
            owner.cancel()
        await _turns()
        if follower_budget is None or follower_budget >= 1:
            assert not follower.done()
        assert scenario.broker.paused
        release.set()
        outcomes = await asyncio.gather(owner, follower, return_exceptions=True)
        assert type(outcomes[0]) is TransitionAdmissionExpired
        if follower_budget is not None and follower_budget < 1:
            assert type(outcomes[1]) is TransitionAdmissionWaitExpired
        else:
            assert outcomes[0] is outcomes[1]
        assert scenario.world.activations == []
        assert scenario.actuator.intents == []
        assert not scenario.broker.paused
        assert scenario.leases.admission("router") is AnimatorAdmission.OPEN
        assert scenario.manager.containment_reason is None
    finally:
        release.set()
        for task in (owner, follower):
            if not task.done():
                task.cancel()
        await asyncio.gather(owner, follower, return_exceptions=True)
