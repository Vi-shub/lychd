"""Authenticated preference refusal is safe and precedes durable admission."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from lychd.agents.workflows import BRIDGE_CHAT_BOUND, BuiltinWorkflowRegistry
from lychd.domain.cortex.bridge_preference import (
    BridgePreference,
    BridgePreferenceCode,
    BridgePreferenceDecision,
    BridgePreferenceError,
)
from lychd.domain.cortex.engine import QueueRouter, RunEngine

if TYPE_CHECKING:
    from types import SimpleNamespace

    from litestar import Litestar
    from litestar.testing import TestClient


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
    assert "private" not in response.text
    restored = asyncio.run(fake_services.bridge_sessions.get_session(session.id))
    assert restored is not None
    assert restored.turns == []
    assert asyncio.run(fake_services.ledger.list_for_session(session.id)) == []
