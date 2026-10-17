"""Orb retains admission preferences without deriving current routing truth."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from lychd.agents.router import Intent
from lychd.agents.workflows.bridge_chat import BRIDGE_CHAT_BOUND
from lychd.domain.animation.capabilities import CapabilityPhase
from lychd.domain.cortex.bridge_preference import BridgePreferenceDecision
from lychd.domain.cortex.runs import RunStatus

if TYPE_CHECKING:
    from litestar import Litestar
    from litestar.testing import TestClient


def _decision() -> BridgePreferenceDecision:
    started = datetime(2026, 10, 17, 12, tzinfo=UTC)
    policy = {
        "schema_version": 1,
        "routes": [
            {"capability_key": "a:chat:model-a", "weight": 9},
            {"capability_key": "b:chat:model-b", "weight": 1},
        ],
    }
    digest = sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return BridgePreferenceDecision.model_validate(
        {
            "policy": policy,
            "policy_digest": digest,
            "batch_started_at": started,
            "batch_finished_at": started + timedelta(seconds=1),
            "observations": [
                {
                    "capability_key": "a:chat:model-a",
                    "phase": CapabilityPhase.COLD,
                    "observed_at": started + timedelta(milliseconds=200),
                    "admission_open": True,
                    "exclusion_code": "not_warm",
                },
                {
                    "capability_key": "b:chat:model-b",
                    "phase": CapabilityPhase.WARM,
                    "observed_at": started + timedelta(milliseconds=800),
                    "admission_open": True,
                    "exclusion_code": None,
                },
            ],
            "selected_capability_key": "b:chat:model-b",
        }
    )


@pytest.mark.parametrize("preference_enabled", [False, True])
def test_orb_projects_only_retained_preference_with_snapshot_stream_parity(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    *,
    preference_enabled: bool,
) -> None:
    decision = _decision() if preference_enabled else None

    async def seed() -> str:
        run = await fake_services.ledger.create(
            Intent(
                session_id="preference-session",
                prompt="private preference prompt",
                sigil_name="private preference principal",
                admitted_capability_key="b:chat:model-b",
                admitted_bridge_preference=decision,
            ),
            workflow_name=BRIDGE_CHAT_BOUND.name,
            pattern_manifest=BRIDGE_CHAT_BOUND.manifest.snapshot(),
            queue_name="runs",
            priority=50,
        )
        await fake_services.ledger.set_status(run.run_id, RunStatus.RUNNING)
        await fake_services.ledger.set_status(run.run_id, RunStatus.DONE)
        return run.run_id

    run_id = asyncio.run(seed())
    # Reading is independent of the current configuration, registry and readiness.
    fake_services.registry = object()
    fake_services.dispatcher = object()
    response = altar_client.get(f"/api/v1/orb/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    summary = body["bridge_preference"]
    if decision is None:
        assert summary is None
    else:
        assert set(summary) == {
            "policy_schema_version",
            "policy_digest",
            "batch_started_at",
            "batch_finished_at",
            "selected_capability_key",
            "routes",
        }
        assert summary["policy_schema_version"] == 1
        assert summary["policy_digest"] == decision.policy_digest
        assert summary["selected_capability_key"] == "b:chat:model-b"
        assert [(row["weight"], row["phase"], row["exclusion_code"]) for row in summary["routes"]] == [
            (9, "cold", "not_warm"),
            (1, "warm", None),
        ]
        assert all(
            set(row)
            == {
                "capability_key",
                "weight",
                "phase",
                "observed_at",
                "admission_open",
                "exclusion_code",
            }
            for row in summary["routes"]
        )
        assert "policy" not in summary
    stream = altar_client.get(f"/api/v1/orb/runs/{run_id}/events")
    assert stream.status_code == 200
    envelopes = [
        json.loads(line.removeprefix("data: ")) for line in stream.text.splitlines() if line.startswith("data: ")
    ]
    assert len(envelopes) == 1
    assert envelopes[0]["kind"] == "settled"
    assert envelopes[0]["snapshot"]["bridge_preference"] == summary
    for text in (response.text, stream.text):
        assert "private preference prompt" not in text
        assert "private preference principal" not in text
