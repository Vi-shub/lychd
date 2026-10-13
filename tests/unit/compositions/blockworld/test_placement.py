"""Owned guards: raw types, custody attribution and bounded policy decisions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from lychd.compositions.blockworld.ports import ActionSnapshot, MissionSnapshot
from lychd.compositions.blockworld.review import prepare_binding
from lychd.compositions.blockworld.schemas import (
    AdmittedPlacementV1,
    PlacementInputError,
    PlacementIntegrityError,
    PlacementStateV1,
    canonical_bytes,
    decode_request,
)
from lychd.compositions.blockworld.services import exact_policy_eligible, prepare_command, validate_action
from lychd.compositions.blockworld.workflow import placement_workflow
from lychd.db.blockworld import (
    _mission,  # pyright: ignore[reportPrivateUsage] - directly exercise retained-row corruption guard
)
from lychd.db.models.blockworld import BlockworldPlacementMission


def identity(number: int) -> str:
    return str(UUID(int=number))


def admitted_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request": {
            "schema_version": 1,
            "request_id": identity(1),
            "mission_id": identity(2),
            "action_id": identity(3),
            "expected_cursor": 0,
            "target": [0, 64, 0],
            "block_state": {"name": "test:stone", "properties": {"axis": "y"}},
            "inventory_change": {
                "slot": "hotbar.1",
                "item": "test:stone",
                "before_count": 2,
                "delta": -1,
                "after_count": 1,
            },
        },
        "principal": {"name": "builder", "scopes": ["blockworld:place"]},
        "pattern_snapshot": placement_workflow(AsyncMock()).manifest.snapshot(),
        "server_id": identity(4),
        "world_id": identity(5),
        "world_epoch": 2,
        "bot_id": identity(6),
        "capability_revision": "test.1",
        "capabilities": ["place_block"],
        "blueprint_id": identity(7),
        "blueprint_digest": "1" * 64,
        "plot": {"id": identity(8), "min": [-1, 63, -1], "max": [1, 65, 1]},
        "lease": {
            "id": identity(9),
            "generation": 2,
            "expires_at": "2035-01-01T12:02:00Z",
            "bot_id": identity(6),
            "plot_id": identity(8),
            "effect": "place_block",
        },
        "mission_policy": {"id": identity(10), "generation": 3},
        "authority_generation": 4,
        "audience": ["builder"],
        "audience_digest": "2" * 64,
        "deadline": "2035-01-01T12:01:00Z",
        "target_before_digest": "3" * 64,
        "inventory_generation": 5,
        "inventory_digest": "4" * 64,
        "judgment_mode": "live",
        "policy_id": None,
    }


def admission() -> AdmittedPlacementV1:
    return AdmittedPlacementV1.model_validate(admitted_document())


def prepared_pair() -> tuple[MissionSnapshot, ActionSnapshot]:
    fixture = admission()
    command = prepare_command(fixture, identity(11))
    binding, review = prepare_binding(command, fixture)
    mission = MissionSnapshot(admission=fixture, run_id=identity(11))
    action = ActionSnapshot(
        action_id=identity(3),
        mission_id=identity(2),
        run_id=identity(11),
        consent_id=identity(12),
        command_bytes=canonical_bytes(command),
        preparation_digest=binding.preparation_digest,
        review_bytes=review.canonical_bytes(),
        native_binding=binding,
        status="prepared",
    )
    return mission, action


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", "1"),
        ("expected_cursor", False),
        ("expected_cursor", 0.0),
    ],
)
def test_original_integer_types_refused(field: str, value: object) -> None:
    request = admitted_document()["request"]
    request[field] = value
    with pytest.raises(PlacementInputError):
        decode_request(request)


def test_nested_wire_and_duplicate_keys_refused() -> None:
    request = admitted_document()["request"]
    request["inventory_change"]["before_count"] = True
    with pytest.raises(PlacementInputError):
        decode_request(request)
    encoded = (
        json.dumps(admitted_document()["request"])
        .replace('"schema_version": 1', '"schema_version": 1,"schema_version": 1')
        .encode()
    )
    with pytest.raises(PlacementInputError):
        decode_request(encoded)


def test_command_and_admission_nested_values_are_immutable() -> None:
    fixture = admission()
    command = prepare_command(fixture, identity(11))
    with pytest.raises(TypeError):
        cast("Any", command.block_state.properties)["axis"] = "x"
    with pytest.raises(TypeError):
        fixture.pattern_snapshot["nodes"][0]["key"] = "other"
    assert canonical_bytes(command) == canonical_bytes(prepare_command(fixture, identity(11)))


@pytest.mark.parametrize("node", [7, {"key": "bad", "label": "Bad", "kind": "unknown_kind"}])
def test_admission_rejects_rehashed_malformed_pattern_before_freezing(node: object) -> None:
    document = admitted_document()
    snapshot = document["pattern_snapshot"]
    snapshot["nodes"].append(node)
    unsigned = {key: value for key, value in snapshot.items() if key != "digest"}
    snapshot["digest"] = sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    with pytest.raises(ValidationError, match="Invalid immutable Pattern snapshot"):
        AdmittedPlacementV1.model_validate(document)


def test_actual_type_adapter_checks_before_coercion_and_serialization() -> None:
    document = {
        "schema_version": 1,
        "run_id": identity(11),
        "session_id": "s",
        "action_id": None,
        "preparation_digest": None,
        "pending_consent_id": None,
        "native_binding_digest": None,
        "result": None,
    }
    adapter = TypeAdapter(PlacementStateV1)
    bad = {**document, "schema_version": True}
    with pytest.raises(ValidationError):
        adapter.validate_json(json.dumps(bad))
    state = adapter.validate_json(json.dumps(document))
    cast("Any", state).schema_version = True
    with pytest.raises(ValueError, match="Error calling function"):
        adapter.dump_json(state)


def test_empty_retained_result_bytes_are_corruption() -> None:
    fixture = admission()
    admitted = canonical_bytes(fixture)
    request = canonical_bytes(fixture.request)
    row = BlockworldPlacementMission(
        id=UUID(identity(2)),
        request_id=UUID(identity(1)),
        action_id=UUID(identity(3)),
        run_id=UUID(identity(11)),
        admission_bytes=admitted,
        admission_digest=sha256(admitted).hexdigest(),
        request_bytes=request,
        request_digest=sha256(request).hexdigest(),
        pattern_snapshot=fixture.model_dump(mode="json")["pattern_snapshot"],
        pattern_digest=fixture.pattern_snapshot["digest"],
        cursor=0,
        placements_remaining=1,
        pending_action_id=None,
        result_bytes=b"",
    )
    with pytest.raises(PlacementIntegrityError):
        _mission(row)


def test_pending_requires_complete_retained_judgment() -> None:
    mission, action = prepared_pair()
    pending = replace(
        action, status="pending", claim_id=identity(13), claimed_at=datetime.now(UTC), issuance_judgment={}
    )
    with pytest.raises(PlacementIntegrityError):
        validate_action(pending, mission)
    with pytest.raises(PlacementIntegrityError):
        validate_action(replace(action, receipt_digest="0" * 64), mission)


def test_exact_policy_requires_singleton_allowlists_and_one_use() -> None:
    command = prepare_command(admission(), identity(11))
    _, action = prepared_pair()
    payload = {"action_id": identity(3), "mission_id": identity(2), "preparation_digest": action.preparation_digest}
    policy = SimpleNamespace(
        enabled=True,
        source_present=True,
        sigil_pattern="builder",
        tool_pattern="blockworld.place_block",
        constraints={"args": {key: [value] for key, value in payload.items()}},
        expires_at=datetime(2035, 1, 1, 12, 1, tzinfo=UTC),
        max_uses=1,
        uses=0,
    )
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    assert exact_policy_eligible(policy, command, now)
    policy.constraints = {"args": payload}
    assert not exact_policy_eligible(policy, command, now)
    policy.constraints = {"args": {key: [value] for key, value in payload.items()}}
    policy.uses = 1
    assert not exact_policy_eligible(policy, command, now)
    assert exact_policy_eligible(policy, command, now, consumed=True)


@pytest.mark.asyncio
async def test_actual_stasis_refuses_corrupt_live_state_before_replacing_checkpoint() -> None:
    from lychd.compositions.blockworld.workflow import PreparePlacement
    from lychd.domain.cortex.stasis import DurableStasisPhylactery, InMemoryStasisStore

    store = InMemoryStasisStore()
    persistence = DurableStasisPhylactery(job_id=identity(11), store=store)
    persistence.set_graph_types(placement_workflow(AsyncMock()).graph)
    state = PlacementStateV1(
        schema_version=1,
        run_id=identity(11),
        session_id="s",
        action_id=None,
        preparation_digest=None,
        pending_consent_id=None,
        native_binding_digest=None,
        result=None,
    )
    await persistence.snapshot_node(state, PreparePlacement())
    retained = await store.load(identity(11))
    assert retained is not None
    cast("Any", state).schema_version = 1.0
    with pytest.raises(ValueError, match="Error calling function"):
        await persistence.snapshot_node(state, PreparePlacement())
    assert await store.load(identity(11)) == retained
    retained[0]["state"]["schema_version"] = True
    await store.replace(identity(11), retained)
    with pytest.raises(ValidationError):
        await persistence.load_all()


@pytest.mark.asyncio
async def test_preinvoke_integrity_failure_is_not_converted_to_unknown() -> None:
    from unittest.mock import AsyncMock

    from lychd.compositions.blockworld.ports import NewClaim
    from lychd.compositions.blockworld.services import PlacementService
    from lychd.domain.codex.sigil import Sigil

    _, action = prepared_pair()
    store = SimpleNamespace(
        claim_issue=AsyncMock(
            return_value=NewClaim(action_id=action.action_id, claim_id=identity(13), command_bytes=action.command_bytes)
        ),
        read_action=AsyncMock(return_value=action),
        validate_live_claim_before_invoke=AsyncMock(side_effect=PlacementIntegrityError("changed binding")),
        record_unknown=AsyncMock(),
    )
    effect = SimpleNamespace(invoke=AsyncMock())
    service = PlacementService(cast("Any", store), AsyncMock(), cast("Any", effect))
    with pytest.raises(PlacementIntegrityError, match="changed binding"):
        await service.execute(
            action.action_id, action.native_binding, Sigil(name="builder", scopes=frozenset({"blockworld:place"}))
        )
    effect.invoke.assert_not_awaited()
    store.record_unknown.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_engine_held_replay_with_unreadable_delivery_is_unresolved() -> None:
    from unittest.mock import AsyncMock

    from lychd.agents.router import Intent
    from lychd.agents.workflows import BuiltinWorkflowRegistry
    from lychd.compositions.blockworld.schemas import PlacementNotFoundError
    from lychd.compositions.blockworld.services import PlacementService
    from lychd.domain.codex.sigil import Sigil
    from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
    from lychd.domain.cortex.events import InProcessEventBus
    from lychd.domain.cortex.ledger import InMemoryRunLedger

    ledger = InMemoryRunLedger()
    fixture = admission()
    intent = Intent(
        session_id="s",
        prompt="Place one block on the admitted plot",
        source="blockworld.placement",
        sigil_name="builder",
        sigil_scopes=frozenset({"blockworld:place"}),
    )
    run, _ = await ledger.create_idempotent(
        intent,
        idempotency_key=identity(1),
        workflow_name="blockworld.bounded_mission",
        pattern_manifest=placement_workflow(AsyncMock()).manifest.snapshot(),
        queue_name="runs",
        priority=50,
        hold_delivery=True,
    )
    store = SimpleNamespace(
        read_mission=AsyncMock(return_value=MissionSnapshot(admission=fixture, run_id=run.run_id)),
        read_action=AsyncMock(side_effect=PlacementNotFoundError("absent")),
        retain_run=AsyncMock(),
    )
    service = PlacementService(cast("Any", store), AsyncMock(), AsyncMock())
    engine = RunEngine(
        ledger=ledger,
        bus=InProcessEventBus(ledger=ledger),
        workflows=BuiltinWorkflowRegistry(workflows=(placement_workflow(service),)),
        queue_router=QueueRouter(routing={"default": RouteRule(queue="runs", priority=50)}),
        queues={"runs": AsyncMock()},
    )
    ledger.get_delivery = AsyncMock(side_effect=RuntimeError("custody unavailable"))
    result = await service.submit(
        fixture.request,
        engine=engine,
        ledger=ledger,
        session_id="s",
        principal=Sigil(name="builder", scopes=frozenset({"blockworld:place"})),
    )
    assert result == {
        "kind": "admission_unresolved",
        "request_id": identity(1),
        "mission_id": identity(2),
        "run_id": run.run_id,
        "reason": "custody_unavailable",
    }
    store.retain_run.assert_not_awaited()


def _evidence_document(digest: str) -> dict[str, Any]:
    return {
        "kind": "observed",
        "response": {
            "action_id": identity(3),
            "preparation_digest": digest,
            "server_id": identity(4),
            "world_id": identity(5),
            "world_epoch": 2,
            "bot_id": identity(6),
            "response_id": "response.1",
            "accepted": True,
        },
        "event": {
            "action_id": identity(3),
            "preparation_digest": digest,
            "server_id": identity(4),
            "world_id": identity(5),
            "world_epoch": 2,
            "bot_id": identity(6),
            "event_id": "event.1",
            "event_sequence": 7,
            "target": [0, 64, 0],
            "block_state": {"name": "test:stone", "properties": {"axis": "y"}},
            "inventory_after": 1,
            "cursor_before": 0,
            "cursor_after": 1,
        },
    }


def _judgment(action: ActionSnapshot) -> dict[str, Any]:
    return {
        "consent_id": action.consent_id,
        "binding_digest": action.native_binding.digest,
        "verdict": "granted",
        "decided_by": "test:operator",
        "decided_at": "2035-01-01T12:00:00Z",
        "policy_id": None,
        "policy_slug": None,
        "policy_digest": None,
    }


def test_rehashed_receipt_for_different_action_is_never_projected_verified() -> None:
    from lychd.compositions.blockworld.schemas import PlacementEvidence, receipt_digest
    from lychd.compositions.blockworld.services import receipt_for

    mission, action = prepared_pair()
    receipt = receipt_for(
        prepare_command(mission.admission, action.run_id),
        PlacementEvidence.model_validate(_evidence_document(action.preparation_digest)),
        identity(14),
    )
    corrupt = receipt.model_copy(update={"action_id": identity(99)})
    encoded = canonical_bytes(corrupt)
    verified = replace(
        action,
        status="verified",
        claim_id=identity(13),
        claimed_at=datetime(2035, 1, 1, 12, tzinfo=UTC),
        issuance_judgment=_judgment(action),
        receipt_bytes=encoded,
        receipt_digest=receipt_digest(encoded),
    )
    with pytest.raises(PlacementIntegrityError, match="another effect"):
        validate_action(verified, replace(mission, cursor=1, placements_remaining=0))


@pytest.mark.asyncio
async def test_late_observed_evidence_cannot_hide_terminal_custody_conflict() -> None:
    from unittest.mock import AsyncMock

    from lychd.compositions.blockworld.schemas import PlacementEvidence
    from lychd.compositions.blockworld.services import PlacementService, not_issued

    mission, action = prepared_pair()
    refusal = not_issued(action.action_id, "consent_denied")
    terminal = replace(action, status="not_issued", reason="consent_denied")
    store = SimpleNamespace(
        read_action=AsyncMock(return_value=terminal),
        load_for_run=AsyncMock(return_value=replace(mission, result=refusal)),
        settle_verified=AsyncMock(side_effect=PlacementIntegrityError("conflicting terminal evidence")),
        record_unknown=AsyncMock(),
    )
    service = PlacementService(cast("Any", store), AsyncMock(), AsyncMock())
    with pytest.raises(PlacementIntegrityError, match="conflicting terminal evidence"):
        await service._settle(  # pyright: ignore[reportPrivateUsage] - directly exercise late evidence conflict boundary
            action.action_id, PlacementEvidence.model_validate(_evidence_document(action.preparation_digest))
        )
    store.record_unknown.assert_not_awaited()


@pytest.mark.parametrize("change", ["capability", "bot", "plot", "effect"])
def test_admission_requires_explicit_exact_lease_and_effect_permission(change: str) -> None:
    data = admitted_document()
    if change == "capability":
        data["capabilities"] = []
    else:
        data["lease"][{"bot": "bot_id", "plot": "plot_id", "effect": "effect"}[change]] = (
            identity(98) if change != "effect" else "dig_block"
        )
    with pytest.raises(ValidationError):
        AdmittedPlacementV1.model_validate(data)
