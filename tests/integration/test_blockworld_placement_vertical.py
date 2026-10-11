"""Eight prefrozen Blockworld histories through real owned decisions and Graph.

This portable lane models storage commits; PostgreSQL and live game behavior are
not established. Expected bytes were frozen before implementation was read.
"""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from lychd.agents.router import Intent
from lychd.agents.workflows import builtin_workflow_registry
from lychd.compositions.blockworld import schemas, services
from lychd.compositions.blockworld.ports import ExistingDisposition, PreparedAction, Refused
from lychd.compositions.blockworld.schemas import PlacementStateV1
from lychd.domain.codex.sigil import Sigil
from lychd.domain.cortex.engine import RunEngine
from lychd.domain.cortex.runs import RunDeliveryState, RunHandle, RunStatus
from lychd.domain.cortex.stasis import DurableStasisPhylactery
from tests.support.blockworld import BUILDER, IDS, LITERAL, Boot, canonical, expected

pytestmark = pytest.mark.asyncio


def assert_quiet(boot: Boot) -> None:
    assert boot.store.action is None
    assert boot.consents._rows == {}
    assert boot.world.invokes == []
    assert boot.world.effects == 0
    assert boot.store.mission.cursor == 0
    assert boot.store.mission.placements_remaining == 1
    assert boot.store.policy is None or boot.store.policy.uses == 0


async def prepare(mode: str = "live") -> Boot:
    boot = Boot(mode=mode)
    result = await boot.submit()
    assert result["kind"] == "admitted"
    assert boot.run_id != LITERAL["advisory_run_id"]
    assert boot.run_id != LITERAL["sample_only_run_id"]
    if mode != "live":
        boot.install_policy()
    return boot


async def test_o1_real_admission_graph_http_and_resume_use_frozen_bytes(  # noqa: PLR0915 - one connected vertical history
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Path(schemas.__file__).parent.parent.parent.parent == Path(__file__).parents[2] / "src"
    boot = Boot()
    actual_submit = boot.engine.submit
    observed_intents: list[Intent] = []

    async def advisory_submit(_engine: RunEngine, intent: Intent, **kwargs: Any) -> RunHandle:
        actual = intent.model_copy(update={"run_id": LITERAL["advisory_run_id"]})
        observed_intents.append(actual)
        return await actual_submit(actual, **kwargs)

    monkeypatch.setattr(type(boot.engine), "submit", advisory_submit)
    assert (await boot.submit())["kind"] == "admitted"
    assert boot.run_id is not None
    assert observed_intents[0].run_id == LITERAL["advisory_run_id"]
    assert boot.run_id != observed_intents[0].run_id
    assert boot.store.mission.run_id == boot.run_id
    assert boot.run_id is not None
    vector = expected(boot.run_id)
    assert boot.workflow.manifest.snapshot() == LITERAL["candidate_manifest"]
    assert boot.store.retention_deliveries == [RunDeliveryState.HELD]
    assert len(boot.queue.attempts) == 1
    assert_quiet(boot)
    first = await boot.drive()
    assert first["status"] == "awaiting_consent"
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    assert run.status is RunStatus.AWAITING_CONSENT
    assert run.pattern_manifest == LITERAL["candidate_manifest"]
    assert boot.store.action is not None
    action = boot.store.action
    assert action.command_bytes == vector["command_bytes"]
    assert action.review_bytes == vector["review_bytes"]
    assert action.preparation_digest == vector["preparation_digest"]
    assert action.native_binding.digest == vector["binding_digest"]
    assert list(boot.consents._rows) == [IDS["consent"]]
    assert boot.consents._rows[IDS["consent"]].run_id == boot.run_id
    assert action.run_id == boot.run_id
    assert await boot.checkpoints.exists(boot.run_id)
    saved = await boot.checkpoints.load(boot.run_id)
    assert saved is not None
    active = [s for s in saved if s.get("status") == "created"]
    assert len(active) == 1
    assert active[0]["state"]["pending_consent_id"] == IDS["consent"]
    assert active[0]["state"]["preparation_digest"] == vector["preparation_digest"]
    assert boot.world.invokes == []

    monkeypatch.undo()
    original_boot = boot
    boot = Boot(world=original_boot.world, image=await original_boot.image())
    assert boot.run_id is not None
    assert boot.engine is not original_boot.engine
    assert boot.consents is not original_boot.consents
    assert boot.effect is not original_boot.effect
    async with boot.client() as client:
        snapshot = await client.get("/api/v1/bridge")
        assert snapshot.status_code == 200
        card = snapshot.json()["pending_consents"][0]
        assert card["native_review_status"] == "available"
        assert card["native_review"] == {k: v for k, v in vector["review"].items() if k != "schema_version"}
        assert card["review_binding_digest"] == vector["binding_digest"]
        endpoint = f"/api/v1/bridge/consents/{IDS['consent']}/decision"
        missing = await client.post(endpoint, json={"verdict": "approve"})
        assert missing.status_code == 409
        boot.reviewers[0] = Sigil(name="scope-only", scopes=frozenset({"runs:approve"}))
        forbidden = await client.post(
            endpoint, json={"verdict": "approve", "review_binding_digest": vector["binding_digest"]}
        )
        assert forbidden.status_code == 403
        boot.reviewers[0] = Sigil(name="oracle:reviewer", scopes=frozenset({"altar:read", "runs:approve"}))
        response = await client.post(
            endpoint, json={"verdict": "approve", "review_binding_digest": vector["binding_digest"]}
        )
        assert response.status_code == 200
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    assert run.status is RunStatus.QUEUED
    assert (await boot.drive())["status"] == "done"
    assert boot.store.action.receipt_bytes == vector["receipt_bytes"]
    assert boot.store.action.receipt_digest == vector["receipt_digest"]
    assert (boot.store.mission.cursor, boot.store.mission.placements_remaining) == (1, 0)
    assert (len(boot.world.invokes), boot.world.effects) == (1, 1)
    assert boot.dispatcher.calls == []
    assert boot.orchestrator.calls == []
    assert (await boot.submit())["kind"] == "retained_action"
    assert (await boot.drive())["status"] == "skipped"
    assert (len(boot.world.invokes), boot.world.effects) == (1, 1)


@pytest.mark.parametrize(
    ("pointer", "value"),
    [
        (("target", 0), True),
        (("target", 0), 2.0),
        (("target", 0), "2"),
        (("expected_cursor",), False),
        (("schema_version",), True),
        (("inventory_change", "before_count"), True),
        (("action_id",), 3),
        (("mission_id",), "43000000000040008000000000000002"),
        (("block_state", "properties", "axis"), "x"),
        (("metadata",), {}),
        (("inventory_change", "delta"), 0),
        (("target", 0), 4),
    ],
)
async def test_o2_request_refuses_original_wire_before_any_mutation(pointer: tuple[Any, ...], value: Any) -> None:
    boot = Boot()
    request = copy.deepcopy(LITERAL["request"])
    target = request
    for key in pointer[:-1]:
        target = target[key]
    target[pointer[-1]] = value
    with pytest.raises((ValueError, TypeError)):
        await boot.service.submit(
            request, engine=boot.engine, ledger=boot.ledger, session_id=IDS["session"], principal=BUILDER
        )
    assert_quiet(boot)
    assert boot.ledger._runs == {}


async def test_o2_duplicate_size_and_live_instance_guards() -> None:
    raw = canonical(LITERAL["request"])
    for wire in (b'{"schema_version":true,' + raw[1:], raw + b" " * 65536):
        with pytest.raises((ValueError, TypeError)):
            schemas.decode_request(wire)
    value = schemas.decode_command(expected(LITERAL["sample_only_run_id"])["command_bytes"])
    object.__setattr__(value, "schema_version", True)
    with pytest.raises((ValueError, TypeError)):
        schemas.canonical_bytes(value)
    value = schemas.decode_command(expected(LITERAL["sample_only_run_id"])["command_bytes"])
    object.__setattr__(value, "target", (2.0, 64, -1))
    with pytest.raises((ValueError, TypeError)):
        schemas.canonical_bytes(value)
    value = schemas.decode_command(expected(LITERAL["sample_only_run_id"])["command_bytes"])
    with pytest.raises(TypeError):
        # Deliberately attempt mutation through the immutable wire mapping.
        cast("Any", value.block_state.properties)["axis"] = "x"


@pytest.mark.parametrize(
    "failure", ["prepare_after_policy", "prepare_after_consent", "prepare_before_commit", "prepare_after_commit"]
)
async def test_o3_preparation_transaction_acknowledgement(failure: str) -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    boot.store.failure = failure
    with pytest.raises(schemas.PlacementCommitUnknownError):
        await boot.drive()
    if failure != "prepare_after_commit":
        assert_quiet(boot)
    else:
        assert boot.store.action is not None
        assert len(boot.consents._rows) == 1
        assert boot.store.policy.uses == 1
        assert boot.store.broad_policy.uses == 0
        # A lost acknowledgement cannot open a new action or invoke entitlement.
        fresh = Boot(world=boot.world, image=await boot.image())
        assert fresh.run_id is not None
        assert fresh.store.action.action_id == IDS["action"]
        assert fresh.store.policy.uses == 1
        assert len(fresh.consents._rows) == 1


@pytest.mark.parametrize("fault", ["retain_before_commit", "retain_after_commit"])
async def test_o3_failed_retention_replay_is_honest(fault: str) -> None:
    boot = Boot()
    boot.store.failure = fault
    first = await boot.submit()
    assert first["kind"] == "admission_noncompletion"
    assert first["binding"] == ("unbound" if fault == "retain_before_commit" else "bound")
    assert boot.run_id is not None
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    assert run.status is RunStatus.FAILED
    delivery = await boot.ledger.get_delivery(boot.run_id, enqueue_seq=0)
    assert delivery is not None
    assert delivery.state is RunDeliveryState.SETTLED
    boot.store.failure = None
    second = await boot.submit()
    assert second["kind"] == "admission_noncompletion"
    assert len(boot.ledger._runs) == 1
    assert len(boot.store.retention_deliveries) == 1
    assert boot.queue.attempts == []
    assert_quiet(boot)


async def test_o3_mission_only_refusal_stays_terminal_after_policy_appears() -> None:
    boot = Boot(mode="exact_preauth")
    assert (await boot.submit())["kind"] == "admitted"
    assert boot.run_id is not None
    await boot.drive()
    assert_quiet(boot)
    result = boot.store.mission.result
    assert result is not None
    assert result.kind == "not_issued"
    boot.install_policy()
    replay = await boot.submit()
    assert replay["kind"] == "mission_noncompletion"
    assert replay["result"] == result.model_dump(mode="json")
    outcome = await boot.service.prepare(boot.run_id)
    assert isinstance(outcome, Refused)
    assert outcome.result == result
    assert_quiet(boot)


async def test_o4_o5_final_policy_disable_and_competing_unknown_preserve_custody() -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    reached, release = asyncio.Event(), asyncio.Event()
    boot.store.before_final = (reached, release)
    worker = asyncio.create_task(boot.drive())
    await asyncio.wait_for(reached.wait(), 5)
    assert boot.store.action.status == "pending"
    assert boot.store.policy.uses == 1
    # No change to external facts: the Codex authority itself is now absent.
    boot.store.policy.source_present = False
    contender = services.PlacementService(boot.store, boot.authority, boot.world)
    uncertain = await contender.reconcile(IDS["action"])
    assert uncertain.kind == "unverified_effect"
    assert boot.store.action.status == "unverified_effect"
    late = await boot.store.record_not_issued_before_invoke(IDS["action"], IDS["claim"], "policy_changed")
    assert isinstance(late, ExistingDisposition)
    assert late.status == "unverified_effect"
    release.set()
    await worker
    assert boot.store.action.status == "unverified_effect"
    assert (len(boot.world.invokes), boot.world.effects) == (0, 0)
    assert boot.store.policy.uses == 1
    # No status result may confer NewClaim or infer not_issued.
    assert isinstance(
        await boot.store.claim_issue(IDS["action"], boot.store.action.native_binding, BUILDER), ExistingDisposition
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("world_epoch", 8), ("lease_active", False), ("inventory_count", 2), ("cursor", 1), ("placements_remaining", 0)],
)
async def test_o4_original_live_holder_refuses_before_invocation(field: str, value: Any) -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    reached, release = asyncio.Event(), asyncio.Event()
    boot.store.before_final = (reached, release)
    worker = asyncio.create_task(boot.drive())
    await asyncio.wait_for(reached.wait(), 5)
    boot.authority.facts[field] = value
    release.set()
    await worker
    assert boot.store.action.status == "not_issued"
    assert boot.world.invokes == []
    assert boot.store.mission.cursor == 0
    assert boot.store.mission.placements_remaining == 1


async def test_o6_o7_lost_ack_restart_positive_settlement_and_deletion_custody() -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    boot.world.lose_ack = True
    await boot.drive()
    assert boot.store.action.status == "unverified_effect"
    assert (len(boot.world.invokes), boot.world.effects) == (1, 1)
    assert (boot.store.mission.cursor, boot.store.mission.placements_remaining) == (0, 1)
    image = await boot.image()
    fresh = Boot(world=boot.world, image=image)
    assert fresh.run_id is not None
    assert fresh.store is not boot.store
    assert fresh.lock is not boot.lock
    assert fresh.engine is not boot.engine
    assert fresh.authority is not boot.authority
    assert fresh.effect is not boot.effect
    # Simulate permitted Core cleanup while action and independent world persist.
    fresh.ledger._runs.clear()
    fresh.consents._rows.clear()
    await fresh.checkpoints.delete(fresh.run_id)
    result = await fresh.service.reconcile(IDS["action"])
    assert result.kind == "verified"
    vector = expected(fresh.run_id)
    assert fresh.store.action.receipt_bytes == vector["receipt_bytes"]
    assert fresh.store.action.receipt_digest == vector["receipt_digest"]
    assert (fresh.store.mission.cursor, fresh.store.mission.placements_remaining) == (1, 0)
    assert (len(fresh.world.invokes), fresh.world.effects) == (1, 1)
    again = await fresh.service.reconcile(IDS["action"])
    assert again == result
    delayed = await fresh.store.record_unknown(IDS["action"], "late status uncertainty")
    assert delayed == result
    assert fresh.store.policy.uses == 1


@pytest.mark.parametrize("failure", ["settle_before_commit", "settle_after_commit"])
async def test_o7_receipt_cursor_budget_have_one_commit(failure: str) -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    boot.world.lose_ack = True
    await boot.drive()
    boot.store.failure = failure
    with suppress(schemas.PlacementCommitUnknownError):
        await boot.service.reconcile(IDS["action"])
    fresh = Boot(world=boot.world, image=await boot.image())
    assert fresh.run_id is not None
    if failure == "settle_before_commit":
        assert fresh.store.action.receipt_bytes is None
        assert (fresh.store.mission.cursor, fresh.store.mission.placements_remaining) == (0, 1)
    else:
        assert fresh.store.action.receipt_bytes == expected(fresh.run_id)["receipt_bytes"]
        assert (fresh.store.mission.cursor, fresh.store.mission.placements_remaining) == (1, 0)
    result = await fresh.service.reconcile(IDS["action"])
    assert result.kind == "verified"
    assert (len(fresh.world.invokes), fresh.world.effects) == (1, 1)


async def test_o8_default_boot_has_no_placement_route() -> None:
    registry = builtin_workflow_registry()
    assert all(workflow.name != "blockworld.bounded_mission" for workflow in registry.workflows)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("policy_disable", "policy_changed"),
        ("policy_expire", "policy_changed"),
        ("policy_digest", "policy_changed"),
        ("core_cancel", "consent_cancelled"),
    ],
)
async def test_o4_final_local_authority_is_checked_without_competing_reconciliation(change: str, reason: str) -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    reached, release = asyncio.Event(), asyncio.Event()
    boot.store.before_final = (reached, release)
    worker = asyncio.create_task(boot.drive())
    await asyncio.wait_for(reached.wait(), 5)
    assert boot.store.action.status == "pending"
    assert await boot.consents.verdict(IDS["consent"]) is True
    facts = copy.deepcopy(boot.authority.facts)
    if change == "policy_disable":
        boot.store.policy.source_present = False
    elif change == "policy_expire":
        boot.store.now = boot.store.policy.expires_at
    elif change == "policy_digest":
        boot.store.policy.priority += 1
    else:
        await boot.ledger.begin_cancel(boot.run_id)
        run = await boot.ledger.get(boot.run_id)
        assert run is not None
        await boot.ledger.finish_cancel(boot.run_id, enqueue_seq=run.enqueue_seq)
    if change != "core_cancel":
        assert await boot.consents.verdict(IDS["consent"]) is False
    release.set()
    await worker
    assert boot.authority.facts == facts
    assert boot.store.action.status == "not_issued"
    assert boot.store.action.reason == reason
    assert boot.world.invokes == []
    assert boot.store.policy.uses == 1


async def test_o2_actual_stasis_save_and_load_reject_unsanitized_state() -> None:
    from lychd.compositions.blockworld.workflow import AwaitPlacementConsent

    boot = await prepare()
    assert boot.run_id is not None
    await boot.drive()
    persistence = DurableStasisPhylactery(job_id=boot.run_id, store=boot.checkpoints)
    persistence.set_graph_types(boot.workflow.graph)
    original = await boot.checkpoints.load(boot.run_id)
    assert original is not None
    valid = await persistence.load_all()
    state = next(item.state for item in valid if getattr(item, "status", None) == "created")
    assert type(state) is PlacementStateV1
    for field, invalid in (("schema_version", True), ("preparation_digest", None)):
        bad = copy.deepcopy(original)
        for item in bad:
            if item.get("status") == "created":
                item["state"][field] = invalid
        await boot.checkpoints.replace(boot.run_id, bad)
        with pytest.raises((ValidationError, ValueError, TypeError)):
            await persistence.load_all()
    await boot.checkpoints.replace(boot.run_id, original)
    # Deliberately corrupt a live instance before the serializer guard.
    cast("Any", state).schema_version = True
    with pytest.raises((ValueError, TypeError)):
        await persistence.snapshot_node(state, AwaitPlacementConsent())
    assert await boot.checkpoints.load(boot.run_id) == original
    assert boot.world.invokes == []


async def test_o1_live_denial_and_preparation_replay_keep_one_consent() -> None:
    boot = await prepare()
    assert boot.run_id is not None
    await boot.drive()
    outcomes = await asyncio.gather(boot.service.prepare(boot.run_id), boot.service.prepare(boot.run_id))
    for outcome in outcomes:
        assert isinstance(outcome, PreparedAction)
        assert outcome.action_id == IDS["action"]
    assert len(outcomes) == 2
    assert len(boot.consents._rows) == 1
    async with boot.client() as client:
        response = await client.post(
            f"/api/v1/bridge/consents/{IDS['consent']}/decision",
            json={
                "verdict": "deny",
                "review_binding_digest": expected(boot.run_id)["binding_digest"],
            },
        )
        assert response.status_code == 200
    await boot.drive()
    assert boot.store.action.status == "not_issued"
    assert boot.store.action.reason == "consent_denied"
    assert boot.world.invokes == []


async def test_o7_different_attributed_evidence_cannot_replace_a_verified_receipt() -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    await boot.drive()
    assert boot.store.action.status == "verified"
    before = boot.store.action.receipt_bytes
    evidence = copy.deepcopy(expected(boot.run_id)["observed"])
    evidence["event"]["event_id"] = "another-attributed-event"
    with pytest.raises(schemas.PlacementIntegrityError):
        await boot.store.settle_verified(IDS["action"], schemas.PlacementEvidence.model_validate(evidence))
    assert boot.store.action.receipt_bytes == before
    assert boot.store.mission.placements_remaining == 0
    assert (len(boot.world.invokes), boot.world.effects) == (1, 1)


async def test_o6_wrong_attribution_retains_uncertainty_after_restart() -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    boot.world.lose_ack = True
    await boot.drive()
    fresh = Boot(world=boot.world, image=await boot.image())
    assert fresh.run_id is not None
    assert fresh.world.observed is not None
    fresh.world.observed["event"]["world_epoch"] = 8
    result = await fresh.service.reconcile(IDS["action"])
    assert result.kind == "unverified_effect"
    assert result.reason == "insufficient_attribution"
    assert fresh.store.action.receipt_bytes is None
    assert (fresh.store.mission.cursor, fresh.store.mission.placements_remaining) == (0, 1)
    assert (len(fresh.world.invokes), fresh.world.effects) == (1, 1)


async def test_o3_unknown_retention_ack_resolves_the_committed_binding() -> None:
    boot = Boot()
    boot.store.failure = "retain_after_unknown"
    assert (await boot.submit())["kind"] == "admitted"
    assert boot.run_id is not None
    assert boot.store.mission.run_id == boot.run_id
    assert len(boot.queue.attempts) == 1
    assert len(boot.ledger._runs) == 1


async def test_o3_process_loss_replays_actual_held_admission() -> None:
    from tests.support.blockworld import SimulatedProcessLoss

    boot = Boot()
    boot.store.failure = "retain_process_loss"
    with pytest.raises(SimulatedProcessLoss):
        await boot.submit()
    assert len(boot.ledger._runs) == 1
    observed = next(iter(boot.ledger._runs.values()))
    boot.run_id = observed.run_id
    delivery = await boot.ledger.get_delivery(boot.run_id, enqueue_seq=0)
    assert delivery is not None
    assert delivery.state is RunDeliveryState.HELD
    assert boot.store.mission.run_id is None
    fresh = Boot(world=boot.world, image=await boot.image())
    assert fresh.run_id is not None
    assert (await fresh.submit())["kind"] == "admitted"
    assert fresh.store.mission.run_id == observed.run_id
    assert len(fresh.ledger._runs) == 1
    assert len(fresh.queue.attempts) == 1
    assert_quiet(fresh)


async def test_mission_only_result_actual_stasis_roundtrip_and_mutated_output() -> None:
    from pydantic_graph import End

    from lychd.compositions.blockworld.workflow import ProjectPlacementResult

    boot = Boot(mode="exact_preauth")
    await boot.submit()
    assert boot.run_id is not None
    await boot.drive()
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    state = boot.workflow.make_state(run.to_intent())
    assert isinstance(state, PlacementStateV1)
    assert boot.store.mission.result is not None
    state.result = boot.store.mission.result
    assert state.action_id is None
    persistence = DurableStasisPhylactery(job_id=boot.run_id, store=boot.checkpoints)
    persistence.set_graph_types(boot.workflow.graph)
    await persistence.snapshot_node(state, ProjectPlacementResult())
    restored = await persistence.load_all()
    assert restored[0].state.result == boot.store.mission.result
    before = await boot.checkpoints.load(boot.run_id)
    assert state.result is not None
    output = schemas.NotIssuedResult.model_validate(state.result.model_dump(mode="json"))
    object.__setattr__(output, "cursor", True)
    with pytest.raises((ValueError, TypeError)):
        await persistence.snapshot_end(state, End(output))
    assert await boot.checkpoints.load(boot.run_id) == before
    assert_quiet(boot)


@pytest.mark.parametrize("failure", ["mission_result_before_commit", "mission_result_after_commit"])
async def test_mission_only_result_commit_uncertainty_is_read_before_retry(failure: str) -> None:
    boot = Boot(mode="exact_preauth")
    await boot.submit()
    assert boot.run_id is not None
    boot.store.failure = failure
    with pytest.raises(schemas.PlacementCommitUnknownError):
        await boot.drive()
    fresh = Boot(world=boot.world, image=await boot.image())
    assert fresh.run_id is not None
    replay = await fresh.submit()
    if failure == "mission_result_after_commit":
        assert replay["kind"] == "mission_noncompletion"
        assert boot.store.mission.result is not None
        assert replay["result"] == boot.store.mission.result.model_dump(mode="json")
    else:
        assert replay["kind"] == "admission_noncompletion"
        assert fresh.store.mission.result is None
    assert_quiet(fresh)


async def test_unavailable_action_custody_is_never_treated_as_confirmed_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot = Boot(mode="exact_preauth")
    await boot.submit()
    assert boot.run_id is not None
    await boot.drive()

    async def unavailable(_action_id: str) -> Any:
        raise schemas.PlacementUnavailableError

    monkeypatch.setattr(boot.store, "read_action", unavailable)
    assert (await boot.submit())["kind"] == "admission_unresolved"
    assert_quiet(boot)


async def test_o5_lost_claim_ack_does_not_reconstruct_an_invoke_entitlement() -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    boot.store.failure = "claim_after_commit"
    await boot.drive()
    assert boot.store.action.status == "unverified_effect"
    assert boot.store.action.claim_id == IDS["claim"]
    assert boot.world.invokes == []
    assert boot.world.effects == 0
    assert boot.world.status_calls == 1


@pytest.mark.parametrize("missing", ["run", "consent"])
async def test_o8_prepared_action_cannot_issue_after_core_cleanup(missing: str) -> None:
    boot = await prepare()
    assert boot.run_id is not None
    await boot.drive()
    action = boot.store.action
    if missing == "run":
        boot.ledger._runs.clear()
    else:
        boot.consents._rows.clear()
    result = await boot.service.execute(action.action_id, action.native_binding, BUILDER)
    assert result.kind == "not_issued"
    assert result.reason == "required_record_missing"
    assert boot.world.invokes == []
    assert boot.store.mission.placements_remaining == 1


async def test_preinvoke_core_binding_corruption_propagates_without_unknown_effect() -> None:
    from dataclasses import replace

    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    reached, release = asyncio.Event(), asyncio.Event()
    boot.store.before_final = (reached, release)
    worker = asyncio.create_task(boot.drive())
    await asyncio.wait_for(reached.wait(), 5)
    row = boot.consents._rows[IDS["consent"]]
    assert row.native_binding is not None
    row.native_binding = replace(row.native_binding, preparation_digest="f" * 64)
    release.set()
    with pytest.raises(schemas.PlacementIntegrityError):
        await worker
    assert boot.store.action.status == "pending"
    assert boot.store.mission.result is None
    assert boot.world.invokes == []
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    assert run.status is RunStatus.FAILED


async def test_unreadable_held_delivery_replay_cannot_report_admitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.blockworld import SimulatedProcessLoss

    boot = Boot()
    boot.store.failure = "retain_bound_process_loss"
    with pytest.raises(SimulatedProcessLoss):
        await boot.submit()
    observed = next(iter(boot.ledger._runs.values()))
    assert boot.store.mission.run_id == observed.run_id
    original_get = boot.ledger.get_delivery
    delivery = await original_get(observed.run_id, enqueue_seq=0)
    assert delivery is not None
    assert delivery.state is RunDeliveryState.HELD

    async def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError

    monkeypatch.setattr(boot.ledger, "get_delivery", unavailable)
    result = await boot.submit()
    assert result["kind"] == "admission_unresolved"
    assert result["run_id"] == observed.run_id
    delivery = await original_get(observed.run_id, enqueue_seq=0)
    assert delivery is not None
    assert delivery.state is RunDeliveryState.HELD
    assert boot.queue.attempts == []
    assert len(boot.store.retention_deliveries) == 1
    assert_quiet(boot)


async def test_concurrent_positive_settlement_does_not_hide_conflicting_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    boot = await prepare("exact_preauth")
    assert boot.run_id is not None
    actual_invoke = boot.effect.invoke

    async def conflicting_reply(command_bytes: bytes) -> Any:
        assert boot.run_id is not None
        await actual_invoke(command_bytes)
        retained = await boot.service.reconcile(IDS["action"])
        assert retained.kind == "verified"
        alternate = copy.deepcopy(expected(boot.run_id)["observed"])
        alternate["event"]["event_id"] = "conflicting-late-event"
        return schemas.PlacementEvidence.model_validate(alternate)

    monkeypatch.setattr(boot.effect, "invoke", conflicting_reply)
    with pytest.raises(schemas.PlacementIntegrityError):
        await boot.drive()
    assert boot.store.action.status == "verified"
    assert boot.store.action.receipt_bytes == expected(boot.run_id)["receipt_bytes"]
    assert (boot.store.mission.cursor, boot.store.mission.placements_remaining) == (1, 0)
    run = await boot.ledger.get(boot.run_id)
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert (len(boot.world.invokes), boot.world.effects) == (1, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capabilities", None),
        ("capabilities", []),
        ("capabilities", ["break_block"]),
        ("lease.bot_id", IDS["world"]),
        ("lease.plot_id", IDS["world"]),
        ("lease.effect", "break_block"),
    ],
)
async def test_trusted_admission_requires_exact_capability_and_lease_effect_binding(field: str, value: Any) -> None:
    from tests.support.blockworld import admitted_document

    document = admitted_document()
    target = document
    keys = field.split(".")
    for key in keys[:-1]:
        target = target[key]
    if value is None:
        del target[keys[-1]]
    else:
        target[keys[-1]] = value
    with pytest.raises((ValueError, TypeError)):
        schemas.AdmittedPlacementV1.model_validate(document)


async def test_positive_evidence_against_retained_nonissuance_is_integrity_failure() -> None:
    boot = await prepare()
    assert boot.run_id is not None
    await boot.drive()
    async with boot.client() as client:
        response = await client.post(
            f"/api/v1/bridge/consents/{IDS['consent']}/decision",
            json={
                "verdict": "deny",
                "review_binding_digest": expected(boot.run_id)["binding_digest"],
            },
        )
        assert response.status_code == 200
    await boot.drive()
    retained = boot.store.mission.result
    assert retained is not None
    assert retained.kind == "not_issued"
    evidence = schemas.PlacementEvidence.model_validate(expected(boot.run_id)["observed"])
    with pytest.raises(schemas.PlacementIntegrityError):
        await boot.service._settle(IDS["action"], evidence)
    assert boot.store.mission.result == retained
    assert boot.store.action.receipt_bytes is None
    assert boot.world.invokes == []
