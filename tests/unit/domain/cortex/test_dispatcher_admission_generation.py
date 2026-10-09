"""Grant admission must survive complete transitions during provider setup."""

from __future__ import annotations

import asyncio
from typing import Self

import pytest
from pydantic_ai.models.test import TestModel

from lychd.domain.animation.capabilities import CapabilityGrant
from lychd.domain.animation.errors import HardwareTransitionRequired
from lychd.domain.cortex.dispatcher import Dispatcher
from lychd.domain.cortex.events import InProcessEventBus
from lychd.domain.cortex.leases import AnimatorAdmission
from tests.capability_workflows import CapabilityScenario, InertModelConnector, build_capability_scenario


def _transition_scenario(transition: str) -> tuple[CapabilityScenario, str, str]:
    if transition == "soft":
        scenario = build_capability_scenario(models={"router": ("small", "large")}, active={"router"})
        return scenario, "router:chat:small", "router:chat:large"
    scenario = build_capability_scenario(
        models={"a": ("a-model",), "b": ("b-model",), "side": ("side-model",)},
        active={"a", "side"},
        coexist=("side",),
    )
    capability_key = "side:chat:side-model" if transition == "unrelated" else "a:chat:a-model"
    return scenario, capability_key, "b:chat:b-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["hard", "soft", "unrelated"])
async def test_completed_transition_during_provider_entry_fences_only_affected_grants(
    monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    """A reopened gate cannot validate the observation from before its drain."""
    scenario, capability_key, target_key = _transition_scenario(transition)
    events = InProcessEventBus()
    dispatcher = Dispatcher(scenario.registry, leases=scenario.leases, events=events)
    animator = capability_key.split(":", maxsplit=1)[0]
    entering = asyncio.Event()
    finish_enter = asyncio.Event()
    closed = asyncio.Event()

    class SlowEnterModel(TestModel):
        async def __aenter__(self) -> Self:
            entering.set()
            await finish_enter.wait()
            return self

        async def __aexit__(self, *_args: object) -> None:
            closed.set()

    model = SlowEnterModel()

    def get_model(_self: InertModelConnector, *, model_id: str | None = None) -> TestModel:
        _ = model_id
        return model

    monkeypatch.setattr(InertModelConnector, "get_model", get_model)
    admitted: list[str] = []

    async def consume() -> None:
        async with dispatcher.lease_grant(
            family="chat", capability_key=capability_key, run_id="provider-entry"
        ) as grant:
            admitted.append(grant.spec.key)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entering.wait(), timeout=1)
        assert scenario.leases.active() == []
        plan = await scenario.manager.request_transition(target_key, 50)
        assert plan.action_type == ("SOFT_SWAP" if transition == "soft" else "HARD_SWAP")
        assert scenario.leases.admission(animator) is AnimatorAdmission.OPEN
        current_state = scenario.registry.get_capability_state(capability_key)
        assert current_state is not None
        assert current_state.warm is (transition == "unrelated")
        finish_enter.set()
        if transition == "unrelated":
            await asyncio.wait_for(consumer, timeout=1)
            assert admitted == [capability_key]
        else:
            with pytest.raises(HardwareTransitionRequired) as error:
                await asyncio.wait_for(consumer, timeout=1)
            assert error.value.capability_key == capability_key
            assert admitted == []
            assert events.snapshot("provider-entry") is None
        assert closed.is_set()
        assert scenario.leases.active() == []
        if transition != "unrelated":
            await scenario.manager.request_transition(capability_key, 50)
            await consume()
            assert admitted == [capability_key]
            assert scenario.leases.active() == []
    finally:
        finish_enter.set()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_transition_during_grant_issue_requires_fresh_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An asynchronous registry cannot return a grant across a completed swap."""
    scenario = build_capability_scenario(active={"a"})
    original_issue = scenario.registry.issue_grant

    async def issue_then_swap(key: str, *, holder: str) -> CapabilityGrant:
        grant = await original_issue(key, holder=holder)
        await scenario.manager.request_transition("b:chat:b-model", 50)
        return grant

    monkeypatch.setattr(scenario.registry, "issue_grant", issue_then_swap)
    with pytest.raises(HardwareTransitionRequired) as error:
        async with scenario.dispatcher.lease_grant(family="chat", capability_key="a:chat:a-model", run_id="issue"):
            pytest.fail("a pre-swap observation cannot admit an evicted capability")
    assert error.value.capability_key == "a:chat:a-model"
    assert scenario.world.active == {"b"}
    assert scenario.leases.admission("a") is AnimatorAdmission.OPEN
    assert scenario.leases.active() == []
