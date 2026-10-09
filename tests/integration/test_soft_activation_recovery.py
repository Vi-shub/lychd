"""Real durable guard and orchestration with inert model effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from lychd.domain.animation.capabilities import ActivationResult
from lychd.domain.cortex.leases import AnimatorAdmission
from lychd.domain.orchestration.actuator import RuntimePreconditionError
from lychd.domain.orchestration.containment import SoftActivationRecord
from lychd.system.services.runtime_guard import FileSoftActivationGuard
from tests.capability_workflows import build_capability_scenario

if TYPE_CHECKING:
    from pathlib import Path

    from lychd.domain.animation.animators import RuntimeAnimator
    from lychd.domain.animation.capabilities import CapabilitySpec


def _guard(tmp_path: Path) -> FileSoftActivationGuard:
    directory = tmp_path / "runtime-guard"
    directory.mkdir(mode=0o700)
    return FileSoftActivationGuard(directory)


@pytest.mark.asyncio
async def test_soft_activation_is_durable_before_invocation_and_clears_only_after_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(tmp_path)
    scenario = build_capability_scenario(
        models={"router": ("small", "large")}, active={"router"}, soft_activation_guard=guard
    )
    original = scenario.world.activate_capability

    async def activate(animator: RuntimeAnimator, spec: CapabilitySpec) -> ActivationResult:
        pending = FileSoftActivationGuard(tmp_path / "runtime-guard").pending()
        assert pending is not None
        assert pending.target_capability_key == spec.key
        assert pending.target_animator == animator.name
        assert scenario.broker.paused
        assert scenario.leases.admission(animator.name) is AnimatorAdmission.DRAINING
        return await original(animator, spec)

    monkeypatch.setattr(scenario.world, "activate_capability", activate)
    await scenario.manager.request_transition("router:chat:large", 80)
    guard.assert_clear()
    assert scenario.world.loaded["router"] == "large"
    assert not scenario.broker.paused


@pytest.mark.asyncio
async def test_failed_soft_activation_fences_a_new_generation_even_when_configuration_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _guard(tmp_path)
    scenario = build_capability_scenario(
        models={"router": ("small", "large")}, active={"router"}, soft_activation_guard=guard
    )

    async def reject(_animator: RuntimeAnimator, _spec: CapabilitySpec) -> ActivationResult:
        return ActivationResult(accepted=False, reason="outcome cannot be proved")

    monkeypatch.setattr(scenario.world, "activate_capability", reject)
    with pytest.raises(RuntimeError, match="Failed to activate"):
        await scenario.manager.request_transition("router:chat:large", 80)
    assert scenario.broker.paused
    assert scenario.leases.admission("router") is AnimatorAdmission.DRAINING
    with pytest.raises(RuntimeError):
        build_capability_scenario(
            models={"router": ("another", "replacement")},
            active={"router"},
            soft_activation_guard=FileSoftActivationGuard(tmp_path / "runtime-guard"),
        )
    assert guard.pending() is not None


@pytest.mark.asyncio
async def test_read_only_soft_failure_leaves_no_durable_attempt(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    scenario = build_capability_scenario(
        models={"router": ("small", "large")}, active={"router"}, soft_activation_guard=guard
    )

    async def fail_observation(_name: str) -> None:
        if scenario.broker.paused:
            msg = "inventory unavailable"
            raise RuntimeError(msg)

    scenario.world.before_probe = fail_observation
    with pytest.raises(RuntimePreconditionError):
        await scenario.manager.request_transition("router:chat:large", 80)
    FileSoftActivationGuard(tmp_path / "runtime-guard").assert_clear()
    assert not scenario.broker.paused
    assert scenario.world.activations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["arm", "clear"])
async def test_guard_storage_failure_keeps_admission_closed(tmp_path: Path, stage: str) -> None:
    directory = tmp_path / "runtime-guard"
    directory.mkdir(mode=0o700)

    class FailingGuard(FileSoftActivationGuard):
        def arm(self, record: SoftActivationRecord) -> None:
            super().arm(record)
            if stage == "arm":
                msg = "storage acknowledgement failed"
                raise OSError(msg)

        def clear(self, record: SoftActivationRecord) -> None:
            _ = record
            msg = "storage acknowledgement failed"
            raise OSError(msg)

    guard = FailingGuard(directory)
    scenario = build_capability_scenario(
        models={"router": ("small", "large")}, active={"router"}, soft_activation_guard=guard
    )
    with pytest.raises(OSError, match="storage acknowledgement failed"):
        await scenario.manager.request_transition("router:chat:large", 80)
    assert scenario.world.activations == ([] if stage == "arm" else ["router:chat:large"])
    assert scenario.manager.containment_reason is not None
    assert scenario.broker.paused
    assert scenario.leases.admission("router") is AnimatorAdmission.DRAINING
    with pytest.raises(RuntimeError):
        FileSoftActivationGuard(directory).assert_clear()
