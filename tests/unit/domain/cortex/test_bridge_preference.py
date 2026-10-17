"""Admission ranks immutable fresh observations without entering a model."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

import pytest
from pydantic import ValidationError

from lychd.domain.animation.capabilities import CapabilityPhase, CapabilitySpec, CapabilityState, SourceKind
from lychd.domain.animation.protocols import CapabilityRegistry
from lychd.domain.animation.schemas.capability_family import CapabilityFamily
from lychd.domain.cortex.bridge_preference import (
    BridgePreference,
    BridgePreferenceCode,
    BridgePreferenceError,
    BridgePreferenceRoute,
    BridgePreferenceSelector,
    validate_retained_preference,
)
from lychd.domain.cortex.leases import LeaseLedger


def _spec(animator: str, model: str = "answer") -> CapabilitySpec:
    return CapabilitySpec(
        key=f"{animator}:chat:{model}",
        animator_name=animator,
        runtime="inert",
        source_kind=SourceKind.SOULSTONE,
        family=CapabilityFamily.CHAT,
        model_id=model,
        supports_tools=True,
    )


def _policy(first: CapabilitySpec, second: CapabilitySpec, weights: tuple[int, int] = (2, 1)) -> BridgePreference:
    return BridgePreference(
        schema_version=1,
        routes=(
            BridgePreferenceRoute(capability_key=first.key, weight=weights[0]),
            BridgePreferenceRoute(capability_key=second.key, weight=weights[1]),
        ),
    )


@dataclass
class _Registry:
    specs: list[CapabilitySpec]
    phases: dict[str, CapabilityPhase] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    after: Callable[[str], None] | None = None
    failure: BaseException | None = None
    malformed: bool = False

    def list_capabilities(self) -> list[CapabilitySpec]:
        return self.specs

    def get_capability(self, key: str) -> CapabilitySpec | None:
        return next((spec for spec in self.specs if spec.key == key), None)

    async def refresh_capability_states_for_animator(self, animator: str) -> list[CapabilityState]:
        self.calls.append(animator)
        if self.failure is not None and animator == "b":
            raise self.failure
        if self.after is not None:
            self.after(animator)
        if self.malformed:
            return []
        return [
            CapabilityState(
                capability_key=spec.key,
                phase=self.phases.get(spec.key, CapabilityPhase.WARM),
                reason="private provider reason",
                health="private endpoint",
            )
            for spec in self.specs
            if spec.animator_name == animator
        ]

    def selector(self, leases: LeaseLedger | None = None) -> BridgePreferenceSelector:
        return BridgePreferenceSelector(cast("CapabilityRegistry", self), leases or LeaseLedger())


@pytest.mark.parametrize(("weights", "selected"), [((2, 1), "b"), ((1, 1), "a"), ((0, 1), "a")])
@pytest.mark.asyncio
async def test_rank_tie_and_zero_use_exact_keys(weights: tuple[int, int], selected: str) -> None:
    first, second = _spec("b"), _spec("a")
    registry = _Registry([first, second])
    policy = _policy(first, second, weights)
    decision = await registry.selector().select(policy)
    assert decision.selected_capability_key == _spec(selected).key
    assert registry.calls == (["a"] if weights[0] == 0 else ["a", "b"])
    assert decision.policy_digest == policy.digest
    serialized = decision.model_dump_json()
    assert "private provider reason" not in serialized
    assert "private endpoint" not in serialized
    reverse = policy.model_copy(update={"routes": tuple(reversed(policy.routes))})
    assert reverse.digest == policy.digest


@pytest.mark.parametrize("weights", [(2, 1), (0, 1)])
@pytest.mark.asyncio
async def test_same_animator_refreshes_once_for_both_routes(weights: tuple[int, int]) -> None:
    first, second = _spec("a", "one"), _spec("a", "two")
    registry = _Registry([first, second])
    decision = await registry.selector().select(_policy(first, second, weights))
    assert registry.calls == ["a"]
    first_observation, second_observation = decision.observations
    assert second_observation.phase is CapabilityPhase.WARM
    if weights[0] == 0:
        assert first_observation.phase is None
        assert first_observation.observed_at is None
        assert first_observation.exclusion_code == "disabled"
        assert decision.selected_capability_key == second.key
    else:
        assert first_observation.phase is CapabilityPhase.WARM


@pytest.mark.parametrize("phase", [CapabilityPhase.COLD, CapabilityPhase.ERROR, CapabilityPhase.UNKNOWN])
@pytest.mark.asyncio
async def test_successful_nonwarm_observation_excludes_higher_rank(phase: CapabilityPhase) -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second], phases={first.key: phase})
    decision = await registry.selector().select(_policy(first, second))
    assert decision.selected_capability_key == second.key
    assert decision.observations[0].exclusion_code == "not_warm"
    assert decision.observations[0].phase is phase


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.asyncio
async def test_postprobe_drain_invalidates_earlier_generation(*, reopen: bool) -> None:
    first, second = _spec("a"), _spec("b")
    leases = LeaseLedger()

    def change(animator: str) -> None:
        if animator == "b":
            leases.begin_drain(["a"])
            if reopen:
                leases.end_drain(["a"])

    registry = _Registry([first, second], after=change)
    decision = await registry.selector(leases).select(_policy(first, second))
    assert decision.selected_capability_key == second.key
    assert decision.observations[0].exclusion_code == "readiness_changed"
    assert decision.observations[0].admission_open is reopen
    assert registry.calls == ["a", "b"]


@pytest.mark.asyncio
async def test_already_closed_route_is_excluded_without_reserving_readiness() -> None:
    first, second = _spec("a"), _spec("b")
    leases = LeaseLedger()
    leases.begin_drain(["a"])
    decision = await _Registry([first, second]).selector(leases).select(_policy(first, second))
    assert decision.selected_capability_key == second.key
    assert decision.observations[0].exclusion_code == "admission_closed"
    assert leases.admission_generation("b") == 0


@pytest.mark.parametrize(
    "change",
    [
        {"supports_tools": False},
        {"source_kind": SourceKind.PORTAL},
        {"family": CapabilityFamily.EMBEDDING},
        {"key": "alias"},
    ],
)
@pytest.mark.asyncio
async def test_every_declaration_is_validated_even_when_disabled(change: dict[str, object]) -> None:
    first, second = _spec("a"), _spec("b")
    invalid = second.model_copy(update=change)
    registry = _Registry([first, invalid])
    with pytest.raises(BridgePreferenceError, match=r"^preference_declaration$"):
        await registry.selector().select(_policy(first, second, (1, 0)))
    assert registry.calls == []


@pytest.mark.asyncio
async def test_declaration_change_after_probe_aborts_batch() -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second])

    def change(animator: str) -> None:
        if animator == "b":
            registry.specs[0] = first.model_copy(update={"runtime": "changed"})

    registry.after = change
    with pytest.raises(BridgePreferenceError, match=r"^preference_declaration_changed$"):
        await registry.selector().select(_policy(first, second))


@pytest.mark.asyncio
async def test_failed_later_probe_never_uses_earlier_warm_observation() -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second], failure=RuntimeError("private endpoint credential body"))
    with pytest.raises(BridgePreferenceError, match=r"^preference_probe_failed$") as caught:
        await registry.selector().select(_policy(first, second))
    assert caught.value.code is BridgePreferenceCode.PROBE_FAILED
    assert registry.calls == ["a", "b"]


@pytest.mark.asyncio
async def test_malformed_batch_and_no_eligible_route_refuse() -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second], malformed=True)
    with pytest.raises(BridgePreferenceError, match=r"^preference_probe_invalid$"):
        await registry.selector().select(_policy(first, second))
    registry.malformed = False
    registry.phases = {first.key: CapabilityPhase.UNKNOWN, second.key: CapabilityPhase.COLD}
    with pytest.raises(BridgePreferenceError, match=r"^preference_no_eligible_route$"):
        await registry.selector().select(_policy(first, second))


@pytest.mark.asyncio
async def test_timeout_is_one_total_cooperative_batch_and_cancellation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second], failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await registry.selector().select(_policy(first, second))

    calls: list[str] = []
    timeouts: list[float | None] = []
    timeout = asyncio.timeout

    def fast_timeout(delay: float | None) -> asyncio.Timeout:
        timeouts.append(delay)
        return timeout(0.01)

    async def suspended(animator: str) -> list[CapabilityState]:
        calls.append(animator)
        if animator == "a":
            return [CapabilityState(capability_key=first.key, phase=CapabilityPhase.WARM)]
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(registry, "refresh_capability_states_for_animator", suspended)
    monkeypatch.setattr("lychd.domain.cortex.bridge_preference.asyncio.timeout", fast_timeout)
    with pytest.raises(BridgePreferenceError, match=r"^preference_timeout$"):
        await registry.selector().select(_policy(first, second))
    assert timeouts == [5.0]
    assert calls == ["a", "b"]


@pytest.mark.parametrize("kind", ["duplicate", "foreign", "invalid_phase"])
@pytest.mark.asyncio
async def test_malformed_returned_state_never_becomes_readiness(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _spec("a"), _spec("b")
    registry = _Registry([first, second])

    async def malformed(_animator: str) -> list[CapabilityState]:
        state = CapabilityState(capability_key=first.key, phase=CapabilityPhase.WARM)
        if kind == "duplicate":
            return [state, state]
        if kind == "foreign":
            return [state.model_copy(update={"capability_key": "foreign:chat:answer"})]
        return [state.model_copy(update={"phase": "fabricated"})]

    monkeypatch.setattr(registry, "refresh_capability_states_for_animator", malformed)
    with pytest.raises(BridgePreferenceError, match=r"^preference_probe_invalid$"):
        await registry.selector().select(_policy(first, second))


@pytest.mark.parametrize(
    ("field", "value"), [("policy_digest", "0" * 64), ("selected_capability_key", "b:chat:answer")]
)
@pytest.mark.asyncio
async def test_unchecked_decision_corruption_is_refused(field: str, value: str) -> None:
    first, second = _spec("a"), _spec("b")
    decision = await _Registry([first, second]).selector().select(_policy(first, second))
    with pytest.raises(BridgePreferenceError, match=r"^preference_binding$"):
        validate_retained_preference(decision.model_copy(update={field: value}), first.key)
    with pytest.raises(BridgePreferenceError, match=r"^preference_binding$"):
        validate_retained_preference(decision, second.key)


@pytest.mark.parametrize("weight", [True, -1, 1_000_001, 1.0, "1"])
def test_weights_are_strict_bounded_integers(weight: object) -> None:
    with pytest.raises(ValidationError):
        BridgePreferenceRoute.model_validate({"capability_key": "a:chat:answer", "weight": weight})
