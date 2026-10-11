from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from litestar import Litestar
from litestar.datastructures import State

from lychd.domain.animation.animators import RuntimeAnimator
from lychd.domain.animation.capabilities import CapabilityPhase, CapabilitySpec, CapabilityState
from lychd.domain.animation.services import registry as registry_module
from lychd.domain.codex.middleware import sigil_auth_middleware
from lychd.domain.cortex.leases import LeaseLedger
from lychd.domain.orchestration.arbiter import (
    TransitionAdmissionExpired,
    TransitionAdmissionWaitExpired,
    TransitionDeclined,
)
from lychd.domain.orchestration.manager import OrchestratorManager
from lychd.domain.orchestration.schema import TransitionPlan
from lychd.interface.api.orchestrator import OrchestratorController
from lychd.interface.web.deps import web_dependencies
from tests.capability_workflows import build_capability_scenario
from tests.web.conftest import TEST_ACCESS_PASSWORD, TEST_AUTHORIZATION


class _RecordingOrchestrator(OrchestratorManager):
    """Small HTTP-boundary collaborator with observable transition requests."""

    def __init__(self) -> None:
        self._contained_reason = None
        self.worker_broker = None
        self.planned_targets: list[str] = []
        self.transition_requests: list[tuple[str, float]] = []

    def list_capability_statuses(self) -> list[dict[str, object]]:
        return [
            {
                "capability_key": "test-cap",
                "animator_name": "test-animator",
                "family": "chat",
                "runtime": "reference",
                "source_kind": "soulstone",
                "is_dynamic": True,
                "phase": "warm",
                "model_id": "test-model",
                "is_static": False,
                "is_active": True,
                "is_available": True,
                "warm": True,
                "health": "ok",
                "reason": None,
                "checked_at": None,
                "dedicated": True,
                "persistent_resident": False,
            }
        ]

    async def calculate_transition_plan(self, target_capability_key: str) -> TransitionPlan:
        self.planned_targets.append(target_capability_key)
        return TransitionPlan(
            total_metabolic_cost=50.0,
            evict_coven_ids=["old-relic"],
            launch_coven_ids=[target_capability_key],
            action_type="HARD_SWAP",
        )

    async def request_transition(  # pyright: ignore[reportIncompatibleMethodOverride] - omitted-option collaborator
        self,
        target_capability_key: str,
        priority: float,
        *,
        trace: object | None = None,
    ) -> TransitionPlan:
        _ = trace
        self.transition_requests.append((target_capability_key, priority))
        return TransitionPlan(
            total_metabolic_cost=10.0,
            evict_coven_ids=[],
            launch_coven_ids=[target_capability_key],
            action_type="SOFT_SWAP",
        )


def _app(orchestrator: OrchestratorManager) -> Litestar:
    services = SimpleNamespace(orchestrator=orchestrator, leases=LeaseLedger())
    return Litestar(
        route_handlers=[OrchestratorController],
        dependencies=web_dependencies,
        middleware=[sigil_auth_middleware(access_password=TEST_ACCESS_PASSWORD)],
        state=State({"services": services}),
    )


@pytest.mark.asyncio
async def test_get_status() -> None:
    transport = httpx.ASGITransport(app=_app(_RecordingOrchestrator()))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.get("/orchestrator/status")

    assert response.status_code == 200
    data = response.json()
    assert data["active_capabilities"] == ["test-cap"]
    assert data["all_capabilities"][0]["phase"] == "warm"
    assert data["mutation_containment"] is None


@pytest.mark.asyncio
async def test_get_plan() -> None:
    orchestrator = _RecordingOrchestrator()
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.get("/orchestrator/solver/plan", params={"target": "new-relic"})

    assert response.status_code == 200
    data = response.json()
    assert data["total_metabolic_cost"] == 50.0
    assert data["action_type"] == "HARD_SWAP"
    assert data["evict_coven_ids"] == ["old-relic"]
    assert orchestrator.planned_targets == ["new-relic"]


@pytest.mark.parametrize("failure", ["missing-substrate", "queue-info"])
@pytest.mark.asyncio
async def test_queues_reports_unavailable_truth(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lychd.interface.api.orchestrator as api

    if failure == "missing-substrate":

        def unavailable() -> object:
            message = "not published"
            raise RuntimeError(message)

        monkeypatch.setattr(api, "get_run_substrate", unavailable)
    else:

        class BrokenQueue:
            async def info(self) -> dict[str, int]:
                message = "broker offline"
                raise OSError(message)

        monkeypatch.setattr(
            api,
            "get_run_substrate",
            lambda: SimpleNamespace(queues={"runs": BrokenQueue()}),
        )

    transport = httpx.ASGITransport(app=_app(_RecordingOrchestrator()))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.get("/orchestrator/queues")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_activate_manual_override() -> None:
    orchestrator = _RecordingOrchestrator()
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post("/orchestrator/activate", params={"target": "target-relic"})

    assert response.status_code == 202
    assert response.json()["action_type"] == "SOFT_SWAP"
    assert orchestrator.transition_requests == [("target-relic", 100)]


class _GatingOrchestrator(OrchestratorManager):
    """A gating orchestrator: HARD_SWAP declined below priority 40 (subclasses for DI validation)."""

    def __init__(self) -> None:
        self.worker_broker = None

    async def request_transition(  # pyright: ignore[reportIncompatibleMethodOverride] - omitted-option collaborator
        self,
        target_capability_key: str,
        priority: float,
        *,
        trace: object | None = None,
    ) -> TransitionPlan:
        _ = trace
        plan = TransitionPlan(
            total_metabolic_cost=1.0,
            evict_coven_ids=["old-relic"],
            launch_coven_ids=[target_capability_key],
            action_type="HARD_SWAP",
        )
        if priority < 40:
            raise TransitionDeclined(plan, priority, 40)
        return plan


def _gating_app() -> Litestar:
    return _app(_GatingOrchestrator())


@pytest.mark.asyncio
async def test_activate_low_priority_hard_swap_returns_409() -> None:
    """POST /activate?priority=25 against a HARD_SWAP → 409 carrying the plan + threshold."""
    transport = httpx.ASGITransport(app=_gating_app())  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        resp = await client.post("/orchestrator/activate", params={"target": "titan", "priority": 25})
    assert resp.status_code == 409
    body = resp.json()
    assert body["threshold"] == 40
    assert body["priority"] == 25
    assert body["plan"]["action_type"] == "HARD_SWAP"


@pytest.mark.parametrize("priority", [-1, 101])
@pytest.mark.asyncio
async def test_activate_rejects_priority_outside_doctrine(priority: int) -> None:
    transport = httpx.ASGITransport(app=_gating_app())  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post(
            "/orchestrator/activate",
            params={"target": "titan", "priority": priority},
        )

    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["-1", "nope", "nan", "inf", "-inf", "1e309"])
async def test_activate_rejects_invalid_admission_duration_before_manager(duration: str) -> None:
    orchestrator = _RecordingOrchestrator()
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post(
            "/orchestrator/activate", params={"target": "test", "admission_timeout_s": duration}
        )
    assert response.status_code == 400
    assert orchestrator.transition_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["a:chat:a-model", "b:chat:b-model"])
async def test_zero_admission_duration_refuses_warm_and_cold_without_manager_mutation(target: str) -> None:
    scenario = build_capability_scenario(active={"a"})
    transport = httpx.ASGITransport(app=_app(scenario.manager))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post("/orchestrator/activate", params={"target": target, "admission_timeout_s": 0})
    assert response.status_code == 409
    assert set(response.json()) == {"code", "detail"}
    assert response.json()["code"] == "transition_admission_expired"
    assert scenario.world.probes == []
    assert scenario.manager.transitions.recent() == ()


@pytest.mark.asyncio
async def test_finite_duration_with_overflowing_computed_cutoff_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    import lychd.interface.api.orchestrator as api

    # Replace only the route module's clock access, leaving asyncio's real loop
    # and HTTP middleware untouched. Both inputs are finite; their sum is not.
    monkeypatch.setattr(api, "asyncio", SimpleNamespace(get_running_loop=lambda: SimpleNamespace(time=lambda: 1e308)))
    orchestrator = _RecordingOrchestrator()
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post(
            "/orchestrator/activate", params={"target": "test", "admission_timeout_s": "1e308"}
        )
    assert response.status_code == 400
    assert orchestrator.transition_requests == []


class _TimedOrchestrator(_RecordingOrchestrator):
    def __init__(self, refusal: TransitionAdmissionExpired | None = None) -> None:
        super().__init__()
        self.deadlines: list[float | None] = []
        self.refusal = refusal

    async def request_transition(
        self,
        target_capability_key: str,
        priority: float,
        *,
        trace: object | None = None,
        admission_deadline: float | None = None,
    ) -> TransitionPlan:
        self.deadlines.append(admission_deadline)
        if self.refusal is not None:
            raise self.refusal
        return await super().request_transition(target_capability_key, priority, trace=trace)


@pytest.mark.asyncio
async def test_duration_becomes_one_absolute_cutoff_and_success_shape_is_unchanged() -> None:
    orchestrator = _TimedOrchestrator()
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    before = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post("/orchestrator/activate", params={"target": "test", "admission_timeout_s": 5})
    after = asyncio.get_running_loop().time()
    assert response.status_code == 202
    assert len(orchestrator.deadlines) == 1
    deadline = orchestrator.deadlines[0]
    assert deadline is not None
    assert before + 5 <= deadline <= after + 5
    assert response.json()["action_type"] == "SOFT_SWAP"
    assert "code" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [TransitionAdmissionExpired(), TransitionAdmissionWaitExpired()])
async def test_admission_refusal_preserves_typed_code_and_detail(refusal: TransitionAdmissionExpired) -> None:
    orchestrator = _TimedOrchestrator(refusal)
    transport = httpx.ASGITransport(app=_app(orchestrator))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        response = await client.post("/orchestrator/activate", params={"target": "test", "admission_timeout_s": 5})
    assert response.status_code == 409
    assert response.json() == {"code": refusal.code, "detail": str(refusal)}


@pytest.mark.asyncio
@pytest.mark.parametrize("dynamic", [False, True], ids=["static", "already-warming"])
async def test_soft_observation_keeps_earlier_admission_response_when_both_timers_are_overdue(
    monkeypatch: pytest.MonkeyPatch, *, dynamic: bool
) -> None:
    scenario = build_capability_scenario(
        models={"router": ("small", "large") if dynamic else ("large",)}, active={"router"}
    )
    loop = asyncio.get_running_loop()
    clock = SimpleNamespace(now=loop.time())
    monkeypatch.setattr(loop, "time", lambda: clock.now)
    monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    polling = asyncio.Event()
    original_probe = scenario.world.probe_capability_states
    closed_probes = 0

    async def warming(animator: RuntimeAnimator, specs: list[CapabilitySpec]) -> list[CapabilityState]:
        nonlocal closed_probes
        states = await original_probe(animator, specs)
        if scenario.broker.paused:
            closed_probes += 1
            if closed_probes > 1:
                polling.set()
                await asyncio.Event().wait()
        return [state.model_copy(update={"phase": CapabilityPhase.WARMING}) for state in states]

    monkeypatch.setattr(scenario.world, "probe_capability_states", warming)
    transport = httpx.ASGITransport(app=_app(scenario.manager))  # pyright: ignore[reportArgumentType]
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver.local", headers={"authorization": TEST_AUTHORIZATION}
    ) as client:
        request = asyncio.create_task(
            client.post("/orchestrator/activate", params={"target": "router:chat:large", "admission_timeout_s": 0.01})
        )
        try:
            await polling.wait()
            # The scenario's warm-up budget is 0.2 seconds. A delayed event-loop
            # turn observes both timers due, with admission still the first one.
            clock.now += 1
            response = await request
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert response.status_code == 409
    assert response.json()["code"] == "transition_admission_expired"
    assert scenario.world.activations == []
    assert scenario.actuator.intents == []
    assert not scenario.broker.paused
    assert scenario.manager.containment_reason is None
