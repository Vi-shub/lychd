"""Safe preference refusals cover fresh selection and retained admission replay."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from lychd.agents.workflows import BRIDGE_CHAT_BOUND, BuiltinWorkflowRegistry
from lychd.domain.cortex.bridge_preference import (
    BridgePreference,
    BridgePreferenceCode,
    BridgePreferenceDecision,
    BridgePreferenceError,
)
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine, RunQueue

if TYPE_CHECKING:
    from types import SimpleNamespace

    from litestar import Litestar
    from litestar.testing import TestClient


_REFUSAL_GUIDANCE = (
    "The Bridge could not confirm this request. Check the existing session before sending another message."
)


@pytest.mark.parametrize(
    "code", [BridgePreferenceCode.NO_ELIGIBLE_ROUTE, BridgePreferenceCode.TIMEOUT, "private endpoint credential body"]
)
def test_preference_refusal_returns_safe_503_without_run_or_user_turn(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    code: BridgePreferenceCode | str,
) -> None:
    policy = BridgePreference.model_validate(
        {
            "schema_version": 1,
            "routes": [
                {"capability_key": "a:chat:answer", "weight": 2},
                {"capability_key": "b:chat:answer", "weight": 1},
            ],
        }
    )

    async def refuse(_policy: BridgePreference) -> BridgePreferenceDecision:
        raise BridgePreferenceError(code)

    fake_services.run_engine = RunEngine(
        ledger=fake_services.ledger,
        bus=fake_services.bus,
        workflows=BuiltinWorkflowRegistry(workflows=(BRIDGE_CHAT_BOUND,)),
        queue_router=QueueRouter(routing={}),
        queues={},
        bridge_preference=policy,
        bridge_preference_selector=refuse,
    )
    session = asyncio.run(fake_services.bridge_sessions.create_session())
    response = altar_client.post(
        f"/api/v1/bridge/sessions/{session.id}/messages",
        json={
            "prompt": "private prompt sentinel",
            "request_id": "e4685fa3-861c-44a2-94bd-2ffddfe4f686",
        },
    )
    assert response.status_code == 503
    expected = code if isinstance(code, BridgePreferenceCode) else BridgePreferenceCode.UNAVAILABLE
    assert response.json()["extra"] == {"code": expected.value}
    assert response.json()["detail"] == _REFUSAL_GUIDANCE
    assert "private" not in response.text
    restored = asyncio.run(fake_services.bridge_sessions.get_session(session.id))
    assert restored is not None
    assert restored.turns == []
    assert asyncio.run(fake_services.ledger.list_for_session(session.id)) == []


def test_preference_binding_refusal_preserves_existing_admission_and_turn(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
) -> None:
    now = datetime.now(UTC)
    policy = BridgePreference.model_validate(
        {
            "schema_version": 1,
            "routes": [
                {"capability_key": "a:chat:answer", "weight": 2},
                {"capability_key": "b:chat:answer", "weight": 1},
            ],
        }
    )
    decision = BridgePreferenceDecision.model_validate(
        {
            "policy": policy,
            "policy_digest": policy.digest,
            "batch_started_at": now,
            "batch_finished_at": now,
            "observations": [
                {
                    "capability_key": route.capability_key,
                    "phase": "warm",
                    "observed_at": now,
                    "admission_open": True,
                    "exclusion_code": None,
                }
                for route in policy.routes
            ],
            "selected_capability_key": "a:chat:answer",
        }
    )
    selector = AsyncMock(return_value=decision)
    queue = AsyncMock(spec=RunQueue)
    fake_services.run_engine = RunEngine(
        ledger=fake_services.ledger,
        bus=fake_services.bus,
        workflows=BuiltinWorkflowRegistry(workflows=(BRIDGE_CHAT_BOUND,)),
        queue_router=QueueRouter(routing={"default": RouteRule(queue="runs", priority=50)}),
        queues={"runs": queue},
        bridge_preference=policy,
        bridge_preference_selector=selector,
    )
    session = asyncio.run(fake_services.bridge_sessions.create_session())
    url = f"/api/v1/bridge/sessions/{session.id}/messages"
    body = {"prompt": "private retained prompt sentinel", "request_id": "2a66ec79-f024-4848-9191-4d7b70626141"}
    accepted = altar_client.post(url, json=body)
    assert accepted.status_code == 200
    run_id = accepted.json()["run_id"]
    retained_session = asyncio.run(fake_services.bridge_sessions.get_session(session.id))
    assert retained_session is not None
    assert len(retained_session.turns) == 1
    assert retained_session.turns[0].run_id == run_id
    stored = fake_services.ledger._runs[run_id]
    assert stored.admitted_bridge_preference == decision
    assert stored.admitted_capability_key == decision.selected_capability_key
    # Corrupt retained custody, then let the actual idempotent lookup revalidate it.
    private_key = "private-retained:chat:credential-sentinel"
    fake_services.ledger._runs[run_id] = replace(stored, admitted_capability_key=private_key)
    retained_runs = deepcopy(fake_services.ledger._runs)
    retained_delivery = asyncio.run(fake_services.ledger.get_delivery(run_id, enqueue_seq=stored.enqueue_seq))

    refused = altar_client.post(url, json=body)

    assert refused.status_code == 503
    assert refused.json()["extra"] == {"code": BridgePreferenceCode.BINDING.value}
    assert refused.json()["detail"] == _REFUSAL_GUIDANCE
    assert body["prompt"] not in refused.text
    assert private_key not in refused.text
    assert set(fake_services.ledger._runs) == {run_id}
    assert fake_services.ledger._runs == retained_runs
    assert asyncio.run(fake_services.bridge_sessions.get_session(session.id)) == retained_session
    assert asyncio.run(fake_services.ledger.get_delivery(run_id, enqueue_seq=stored.enqueue_seq)) == retained_delivery
    selector.assert_awaited_once_with(policy)
    queue.enqueue.assert_awaited_once()
