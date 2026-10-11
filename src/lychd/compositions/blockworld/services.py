"""Shared placement decisions and orchestration over retained application custody."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, cast

from pydantic import TypeAdapter

from lychd.agents.router import Intent
from lychd.compositions.blockworld.ports import (
    ActionSnapshot,
    ExistingDisposition,
    MissionSnapshot,
    PlacementAuthorityPort,
    PlacementEffectPort,
    PlacementStore,
    PreparedAction,
    Refused,
)
from lychd.compositions.blockworld.schemas import (
    AdmittedPlacementV1,
    CurrentPlacementFacts,
    NotIssuedResult,
    PlacementAdmissionResult,
    PlacementConflictError,
    PlacementEvidence,
    PlacementEvidenceError,
    PlacementIntegrityError,
    PlacementNotFoundError,
    PlacementReceiptV1,
    PlacementRequestV1,
    PlacementResult,
    PlacementUnavailableError,
    PreparedPlacementV1,
    RefusalCode,
    UnknownEvidence,
    UnverifiedEffectResult,
    VerifiedResult,
    canonical_bytes,
    decode_command,
    decode_request,
    evidence_digest,
    placement_digest,
    receipt_digest,
    utc,
)
from lychd.domain.codex.schemas import ConsentView, NativeConsentBinding
from lychd.domain.codex.services import preauth_authorization_digest
from lychd.domain.codex.sigil import Sigil
from lychd.domain.cortex.runs import ConsentPending


def prepare_command(admission: AdmittedPlacementV1, run_id: str) -> PreparedPlacementV1:
    """Freeze the only admitted request against its canonical Run identity."""
    data = admission.model_dump(mode="json")
    request = data["request"]
    command = {
        key: data[key]
        for key in (
            "principal",
            "server_id",
            "world_id",
            "world_epoch",
            "bot_id",
            "capability_revision",
            "blueprint_id",
            "blueprint_digest",
            "plot",
            "lease",
            "mission_policy",
            "authority_generation",
            "deadline",
            "target_before_digest",
            "inventory_generation",
            "inventory_digest",
        )
    }
    command["lease"] = {key: data["lease"][key] for key in ("id", "generation", "expires_at")}
    command.update(request)
    command.update(
        run_id=run_id,
        operation="place_block",
        composition={"key": "blockworld.inhabitant", "revision": "2"},
        pattern={
            "key": "blockworld.bounded_mission",
            "revision": "2",
            "manifest_digest": data["pattern_snapshot"]["digest"],
        },
        adapter={"key": "inert-placement", "revision": "1"},
        mode="managed",
        placement_budget=1,
        postcondition={
            "target": request["target"],
            "block_state": request["block_state"],
            "inventory_after": request["inventory_change"]["after_count"],
            "cursor": 1,
            "proof": "matching_response_and_authoritative_event",
        },
    )
    return PreparedPlacementV1.model_validate(command)


def validate_mission(mission: MissionSnapshot) -> None:
    canonical_bytes(mission.admission)
    if (
        type(mission.cursor) is not int
        or type(mission.placements_remaining) is not int
        or mission.cursor not in (0, 1)
        or mission.placements_remaining != 1 - mission.cursor
        or mission.pending_action_id not in (None, mission.admission.request.action_id)
    ):
        msg = "Mission limits or pending owner are inconsistent."
        raise PlacementIntegrityError(msg)
    if mission.result is not None:
        canonical_bytes(mission.result)
        if (
            mission.result.action_id != mission.admission.request.action_id
            or mission.result.cursor != mission.cursor
            or mission.result.placements_remaining != mission.placements_remaining
        ):
            msg = "Retained mission result differs from its identity or limits."
            raise PlacementIntegrityError(msg)


def validate_action(action: ActionSnapshot, mission: MissionSnapshot) -> PreparedPlacementV1:  # noqa: C901 - one closed custody check
    """Validate retained bytes and all immutable cross-record bindings."""
    from lychd.compositions.blockworld.review import prepare_binding

    validate_mission(mission)
    command = decode_command(action.command_bytes)
    expected = prepare_command(mission.admission, action.run_id)
    binding, review = prepare_binding(expected, mission.admission)
    claims = (action.claim_id, action.issuance_judgment, action.claimed_at)
    if (
        action.command_bytes != canonical_bytes(expected)
        or action.preparation_digest != placement_digest(action.command_bytes)
        or action.native_binding != binding
        or action.review_bytes != review.canonical_bytes()
        or action.action_id != command.action_id
        or action.mission_id != command.mission_id
        or mission.run_id != action.run_id
        or action.status not in ("prepared", "pending", "not_issued", "unverified_effect", "verified")
        or (any(x is None for x in claims) != all(x is None for x in claims))
        or (action.status == "prepared" and action.claim_id is not None)
        or (action.status in ("pending", "unverified_effect", "verified") and action.claim_id is None)
        or ((action.receipt_bytes is None) != (action.receipt_digest is None))
        or ((action.receipt_bytes is not None and action.receipt_digest is not None) != (action.status == "verified"))
    ):
        msg = "Retained placement identity or disposition is inconsistent."
        raise PlacementIntegrityError(msg)
    if action.claim_id is not None:
        from lychd.compositions.blockworld.schemas import validate_judgment_document

        judgment = action.issuance_judgment
        keys = {
            "consent_id",
            "binding_digest",
            "verdict",
            "decided_by",
            "decided_at",
            "policy_id",
            "policy_slug",
            "policy_digest",
        }
        if (
            judgment is None
            or set(judgment) != keys
            or judgment["consent_id"] != action.consent_id
            or judgment["binding_digest"] != action.native_binding.digest
            or judgment["verdict"] != "granted"
        ):
            msg_0 = "Retained issuance judgment is incomplete or mismatched."
            raise PlacementIntegrityError(msg_0)
        validate_judgment_document(judgment)
        policy_fields = (judgment["policy_id"], judgment["policy_slug"], judgment["policy_digest"])
        if judgment["decided_by"] == "codex:preauth":
            if any(value is None for value in policy_fields) or judgment["policy_id"] != mission.admission.policy_id:
                msg_0 = "Retained automatic judgment lacks exact policy evidence."
                raise PlacementIntegrityError(msg_0)
        elif any(value is not None for value in policy_fields):
            msg_0 = "Live judgment carries inconsistent policy evidence."
            raise PlacementIntegrityError(msg_0)
    if action.status == "verified" and receipt_digest(cast("bytes", action.receipt_bytes)) != action.receipt_digest:
        msg = "Retained receipt digest differs."
        raise PlacementIntegrityError(msg)
    if action.status == "verified":
        receipt = PlacementReceiptV1.model_validate_json(action.receipt_bytes)
        if canonical_bytes(receipt) != action.receipt_bytes:
            msg = "Retained receipt bytes are not canonical."
            raise PlacementIntegrityError(msg)
        for key in ("action_id", "server_id", "world_id", "world_epoch", "bot_id", "target", "block_state"):
            if getattr(receipt, key) != getattr(command, key):
                msg = "Retained receipt names another effect."
                raise PlacementIntegrityError(msg)
        if (
            receipt.preparation_digest != action.preparation_digest
            or receipt.inventory_after != command.inventory_change.after_count
            or mission.cursor != 1
            or mission.placements_remaining != 0
        ):
            msg = "Retained receipt differs from placement postcondition."
            raise PlacementIntegrityError(msg)
    return command


def validate_admitted_run(mission: MissionSnapshot, run: Any) -> None:
    """Require the admitted Principal and exact Pattern on the retained Core Run."""
    if run is None:
        msg = "Required Run is missing."
        raise PlacementIntegrityError(msg)
    run_id = str(getattr(run, "run_id", getattr(run, "id", "")))
    intent = getattr(run, "intent", {})
    scopes = getattr(run, "sigil_scopes", intent.get("sigil_scopes", ()))
    if (
        run_id != mission.run_id
        or run.sigil_name != mission.admission.principal.name
        or frozenset(scopes) != frozenset(mission.admission.principal.scopes)
        or run.pattern_manifest != mission.admission.model_dump(mode="json")["pattern_snapshot"]
    ):
        msg = "Core admission differs from the retained mission."
        raise PlacementConflictError(msg)


def exact_policy_eligible(
    policy: Any,
    command: PreparedPlacementV1,
    now: datetime,
    *,
    consumed: bool = False,
    authorization_digest: str | None = None,
) -> bool:
    """Admit only one exact bounded policy; its own last use remains valid."""
    if policy is None:
        return False
    payload = {
        "action_id": command.action_id,
        "mission_id": command.mission_id,
        "preparation_digest": placement_digest(canonical_bytes(command)),
    }
    return (
        policy.enabled is True
        and policy.source_present is True
        and not any(char in policy.sigil_pattern for char in "*?[")
        and policy.sigil_pattern == command.principal.name
        and policy.tool_pattern == "blockworld.place_block"
        and policy.constraints == {"args": {key: [value] for key, value in payload.items()}}
        and policy.expires_at is not None
        and now < policy.expires_at <= utc(command.deadline)
        and type(policy.max_uses) is int
        and policy.max_uses == 1
        and type(policy.uses) is int
        and policy.uses == (1 if consumed else 0)
        and (authorization_digest is None or preauth_authorization_digest(policy) == authorization_digest)
    )


def authority_refusal(  # noqa: C901, PLR0911, PLR0912 - ordered, exact refusal codes
    command: PreparedPlacementV1,
    facts: CurrentPlacementFacts,
    mission: MissionSnapshot,
    now: datetime,
    principal: Sigil,
) -> RefusalCode | None:
    """Compare external facts with frozen authority and locked local custody."""
    canonical_bytes(facts)
    if (
        principal.name != command.principal.name
        or principal.scopes != frozenset(command.principal.scopes)
        or facts.principal_name != principal.name
    ):
        return "identity_changed"
    if not facts.effect_allowed or not facts.can_place:
        return "authority_revoked"
    if (
        facts.authority_generation != command.authority_generation
        or facts.mission_policy_id != command.mission_policy.id
        or facts.mission_policy_generation != command.mission_policy.generation
    ):
        return "policy_changed"
    if not facts.mission_active or facts.mission_id != command.mission_id:
        return "mission_changed"
    if (facts.server_id, facts.world_id, facts.bot_id, facts.capability_revision) != (
        command.server_id,
        command.world_id,
        command.bot_id,
        command.capability_revision,
    ):
        return "identity_changed"
    if facts.world_epoch != command.world_epoch:
        return "epoch_changed"
    if not facts.can_prove:
        return "proof_capability_missing"
    if (
        not facts.lease_active
        or facts.lease_id != command.lease.id
        or facts.lease_generation != command.lease.generation
        or facts.lease_expires_at != command.lease.expires_at
        or now >= utc(facts.lease_expires_at)
    ):
        return "lease_invalid"
    if now >= utc(command.deadline):
        return "deadline_expired"
    if mission.cursor != command.expected_cursor or facts.cursor != command.expected_cursor:
        return "cursor_changed"
    if mission.placements_remaining != 1 or facts.placements_remaining != 1:
        return "budget_exhausted"
    if facts.target_before_digest != command.target_before_digest:
        return "target_changed"
    if (facts.inventory_generation, facts.inventory_digest, facts.inventory_count) != (
        command.inventory_generation,
        command.inventory_digest,
        command.inventory_change.before_count,
    ):
        return "inventory_changed"
    return None


def consent_refusal(  # noqa: PLR0911 - explicit current-record refusals
    action: ActionSnapshot,
    consent: ConsentView | None,
    run: Any,
    policy: Any,
    now: datetime,
    *,
    authorization_digest: str | None = None,
) -> RefusalCode | None:
    """Validate current local execution and exact first-decision attribution."""
    if run is None or consent is None:
        return "required_record_missing"
    if str(getattr(run, "status", "")) in {"done", "failed", "cancelled", "cancelling"}:
        return "consent_cancelled"
    if (
        consent.id != action.consent_id
        or consent.run_id != action.run_id
        or consent.tool_name != "blockworld.place_block"
        or not consent.native_review_required
        or consent.native_binding != action.native_binding
    ):
        msg = "Consent no longer binds the retained placement."
        raise PlacementIntegrityError(msg)
    if consent.status in {"denied", "expired", "cancelled"}:
        return cast(
            "RefusalCode",
            {"denied": "consent_denied", "expired": "consent_expired", "cancelled": "consent_cancelled"}[
                consent.status
            ],
        )
    if consent.status != "granted" or not consent.decided_by or consent.decided_at is None:
        return "authority_revoked"
    command = decode_command(action.command_bytes)
    if consent.decided_by == "codex:preauth" and consent.preauth_slug is None:
        return "policy_changed"
    if consent.preauth_slug is not None and (
        authorization_digest is None
        or policy is None
        or policy.slug != consent.preauth_slug
        or not exact_policy_eligible(policy, command, now, consumed=True, authorization_digest=authorization_digest)
    ):
        return "policy_changed"
    return None


def not_issued(action_id: str, reason: RefusalCode) -> NotIssuedResult:
    return NotIssuedResult(kind="not_issued", action_id=action_id, reason=reason, cursor=0, placements_remaining=1)


def receipt_for(command: PreparedPlacementV1, evidence: PlacementEvidence, receipt_id: str) -> PlacementReceiptV1:
    """Require independent matching response and authoritative event attribution."""
    canonical_bytes(evidence)
    digest = placement_digest(canonical_bytes(command))
    for observation in (evidence.response, evidence.event):
        if (
            any(
                getattr(observation, key) != getattr(command, key)
                for key in ("action_id", "server_id", "world_id", "world_epoch", "bot_id")
            )
            or observation.preparation_digest != digest
        ):
            msg = "Effect evidence does not attribute this placement."
            raise PlacementEvidenceError(msg)
    event = evidence.event
    if (
        event.target != command.target
        or event.block_state != command.block_state
        or event.inventory_after != command.inventory_change.after_count
    ):
        msg = "Effect evidence does not satisfy the exact postcondition."
        raise PlacementEvidenceError(msg)
    document = {key: getattr(command, key) for key in ("action_id", "server_id", "world_id", "world_epoch", "bot_id")}
    document.update(
        schema_version=1,
        receipt_id=receipt_id,
        preparation_digest=digest,
        adapter_revision="1",
        response_id=evidence.response.response_id,
        authoritative_event_id=event.event_id,
        event_sequence=event.event_sequence,
        target=list(event.target),
        block_state=event.block_state.model_dump(mode="json"),
        inventory_after=event.inventory_after,
        cursor_before=0,
        cursor_after=1,
        placement_cost=1,
        evidence_digest=evidence_digest(canonical_bytes(evidence)),
    )
    return PlacementReceiptV1.model_validate(document)


def result_for(action: ActionSnapshot, mission: MissionSnapshot) -> PlacementResult:
    validate_action(action, mission)
    if action.status == "verified":
        receipt = PlacementReceiptV1.model_validate_json(action.receipt_bytes)
        result = VerifiedResult(
            kind="verified",
            action_id=action.action_id,
            receipt_id=receipt.receipt_id,
            receipt_digest=cast("str", action.receipt_digest),
            cursor=1,
            placements_remaining=0,
        )
    elif action.status == "not_issued":
        result = not_issued(action.action_id, cast("RefusalCode", action.reason))
    elif action.status in {"pending", "unverified_effect"}:
        result = UnverifiedEffectResult(
            kind="unverified_effect",
            action_id=action.action_id,
            reason=cast("Any", action.reason or "acknowledgement_unknown"),
            cursor=0,
            placements_remaining=1,
        )
    else:
        msg = "Prepared action has no terminal result."
        raise PlacementIntegrityError(msg)
    if mission.result is not None and mission.result != result:
        msg = "Action and mission result differ."
        raise PlacementIntegrityError(msg)
    return result


def _admission(document: dict[str, Any]) -> dict[str, Any]:
    return TypeAdapter(PlacementAdmissionResult).validate_python(document).model_dump(mode="json")


class PlacementService:
    """Retain before publish; invoke only with the original committed entitlement."""

    def __init__(self, store: PlacementStore, authority: PlacementAuthorityPort, effect: PlacementEffectPort) -> None:
        """Bind custody and external fact/effect ports explicitly."""
        self.store, self.authority, self.effect = store, authority, effect

    async def prepare(self, run_id: str) -> PreparedAction | ExistingDisposition | Refused:
        return await self.store.prepare_and_admit(run_id)

    async def retained_result(self, action_id: str) -> PlacementResult:
        action = await self.store.read_action(action_id)
        mission = await self.store.load_for_run(action.run_id)
        return result_for(action, mission)

    async def gate(self, action_id: str, principal: Sigil, consents: Any) -> PlacementResult | None:
        action = await self.store.read_action(action_id)
        mission = await self.store.load_for_run(action.run_id)
        command = validate_action(action, mission)
        if principal.name != command.principal.name or principal.scopes != frozenset(command.principal.scopes):
            msg = "Gate Principal differs from retained admission."
            raise PlacementIntegrityError(msg)
        if action.status in {"pending", "unverified_effect"}:
            return await self.reconcile(action_id)
        if action.status != "prepared":
            return result_for(action, mission)
        consent = await consents.get(action.consent_id)
        if consent is None:
            outcome = await self.store.refuse_prepared(action_id, "required_record_missing")
        elif consent.native_binding != action.native_binding or consent.run_id != action.run_id:
            msg = "Gate Consent differs from retained action."
            raise PlacementIntegrityError(msg)
        elif consent.status == "pending":
            raise ConsentPending(action.consent_id, action.run_id, "blockworld.place_block")
        elif consent.status == "granted":
            return None
        else:
            reason = {"denied": "consent_denied", "expired": "consent_expired", "cancelled": "consent_cancelled"}[
                consent.status
            ]
            outcome = await self.store.refuse_prepared(action_id, cast("RefusalCode", reason))
        return outcome.result if isinstance(outcome, Refused) else await self.reconcile(action_id)

    async def execute(  # noqa: PLR0911 - claim, final decision and effect have distinct settlement paths
        self, action_id: str, expected_binding: NativeConsentBinding, principal: Sigil
    ) -> PlacementResult:
        try:
            claim = await self.store.claim_issue(action_id, expected_binding, principal)
        except PlacementUnavailableError:
            return await self.reconcile(action_id)
        if isinstance(claim, Refused):
            return claim.result
        if isinstance(claim, ExistingDisposition):
            return await self.reconcile(action_id)
        try:
            action = await self.store.read_action(action_id)
            if (
                action.command_bytes != claim.command_bytes
                or placement_digest(claim.command_bytes) != action.preparation_digest
            ):
                msg = "Issuance entitlement bytes differ from custody."
                raise PlacementIntegrityError(msg)
            final = await self.store.validate_live_claim_before_invoke(claim, principal)
        except PlacementUnavailableError:
            return await self.reconcile(action_id)
        if isinstance(final, Refused):
            return final.result
        if isinstance(final, ExistingDisposition):
            return await self.reconcile(action_id)
        try:
            # Crossing this await begins the effect; every adapter exception is unknown.
            evidence = await self.effect.invoke(claim.command_bytes)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - port exceptions cannot prove non-issuance
            return await self.store.record_unknown(action_id, "acknowledgement_unknown")
        return await self._settle(action_id, evidence)

    async def _settle(self, action_id: str, evidence: PlacementEvidence | UnknownEvidence) -> PlacementResult:
        if type(evidence) is PlacementEvidence:
            action = await self.store.read_action(action_id)
            mission = await self.store.load_for_run(action.run_id)
            command = validate_action(action, mission)
            if action.status in {"pending", "unverified_effect"}:
                try:
                    receipt_for(command, evidence, action.action_id)
                except PlacementEvidenceError:
                    return await self.store.record_unknown(action_id, "insufficient_attribution")
            try:
                return await self.store.settle_verified(action_id, evidence)
            except PlacementUnavailableError:
                return await self.retained_result(action_id)
        if type(evidence) is not UnknownEvidence:
            msg = "Effect port returned an unsupported observation."
            raise PlacementIntegrityError(msg)
        canonical_bytes(evidence)
        action = await self.store.read_action(action_id)
        if evidence.action_id != action_id or evidence.preparation_digest != action.preparation_digest:
            msg = "Unknown observation names another action."
            raise PlacementIntegrityError(msg)
        return await self.store.record_unknown(action_id, "acknowledgement_unknown")

    async def reconcile(self, action_id: str) -> PlacementResult:
        action = await self.store.read_action(action_id)
        mission = await self.store.load_for_run(action.run_id)
        validate_action(action, mission)
        if action.status in {"verified", "not_issued"}:
            return result_for(action, mission)
        if action.status == "prepared":
            msg = "Prepared action has no issuance to reconcile."
            raise PlacementIntegrityError(msg)
        try:
            evidence = await self.effect.status(action.command_bytes)
        except Exception:  # noqa: BLE001 - unavailable external outcome preserves unknown custody
            return await self.store.record_unknown(action_id, "reconciliation_unavailable")
        return await self._settle(action_id, evidence)

    async def submit(  # noqa: C901, PLR0912, PLR0915 - bounded held-admission reconciliation
        self,
        request: bytes | dict[str, Any] | PlacementRequestV1,
        *,
        engine: Any,
        ledger: Any,
        session_id: str,
        principal: Sigil,
    ) -> dict[str, Any]:
        request = (
            decode_request(canonical_bytes(request))
            if type(request) is PlacementRequestV1
            else decode_request(cast("bytes | dict[str, Any]", request))
        )
        run_id = None
        base = {"request_id": request.request_id, "mission_id": request.mission_id}
        try:
            mission = await self.store.read_mission(request.request_id)
            validate_mission(mission)
            if (
                canonical_bytes(request) != canonical_bytes(mission.admission.request)
                or principal.name != mission.admission.principal.name
                or principal.scopes != frozenset(mission.admission.principal.scopes)
            ):
                msg = "Request or Principal differs from trusted admission."
                raise PlacementConflictError(msg)
            if mission.result is not None and mission.pending_action_id is None and mission.result.kind == "not_issued":
                try:
                    await self.store.read_action(request.action_id)
                except PlacementNotFoundError:
                    return _admission(
                        {
                            "kind": "mission_noncompletion",
                            **base,
                            "run_id": mission.run_id,
                            "result": mission.result.model_dump(mode="json"),
                        }
                    )

            async def retain(canonical_run_id: str) -> None:
                nonlocal run_id
                run_id = canonical_run_id
                for attempt in range(2):
                    try:
                        await self.store.retain_run(request.request_id, canonical_run_id)
                    except PlacementUnavailableError:
                        current = await self.store.read_mission(request.request_id)
                        if current.run_id == canonical_run_id:
                            return
                        if current.run_id is not None:
                            msg = "Admission binds another Run."
                            raise PlacementConflictError(msg) from None
                        if attempt:
                            raise
                    else:
                        current = await self.store.read_mission(request.request_id)
                        if current.run_id != canonical_run_id:
                            msg = "Retained admission was not confirmed."
                            raise PlacementUnavailableError(msg)
                        return

            intent = Intent(
                session_id=session_id,
                prompt="Place one block on the admitted plot",
                source="blockworld.placement",
                sigil_name=principal.name,
                sigil_scopes=principal.scopes,
            )
            try:
                handle = await engine.submit(intent, idempotency_key=request.request_id, retain_before_publish=retain)
                run_id = handle.run_id
            except PlacementConflictError:
                raise
            except Exception:  # noqa: BLE001 - unavailable external outcome preserves unknown custody
                if run_id is None:
                    current_run = await ledger.get_idempotent(intent, idempotency_key=request.request_id)
                    run_id = current_run.run_id if current_run is not None else None
            mission = await self.store.read_mission(request.request_id)
            try:
                action = await self.store.read_action(request.action_id)
            except PlacementNotFoundError:
                action = None
            if action is not None:
                validate_action(action, mission)
                return _admission(
                    {
                        "kind": "retained_action",
                        **base,
                        "run_id": action.run_id,
                        "action_id": action.action_id,
                        "status": action.status,
                    }
                )
            if mission.result is not None:
                return _admission(
                    {
                        "kind": "mission_noncompletion",
                        **base,
                        "run_id": mission.run_id,
                        "result": mission.result.model_dump(mode="json"),
                    }
                )
            run = await ledger.get(run_id) if run_id else None
            if run is None:
                msg = "Canonical admission cannot be established."
                raise PlacementUnavailableError(msg)  # noqa: TRY301 - translate unreadable admission to closed unresolved result
            try:
                delivery = await ledger.get_delivery(run_id, enqueue_seq=run.enqueue_seq)
            except Exception as exc:
                msg = "Canonical delivery cannot be established."
                raise PlacementUnavailableError(msg) from exc
            if delivery is None or delivery.run_id != run_id or delivery.enqueue_seq != run.enqueue_seq:
                msg = "Canonical delivery identity is missing or inconsistent."
                raise PlacementUnavailableError(msg)  # noqa: TRY301 - closed unresolved admission result
            if str(run.status) in {"done", "failed", "cancelled"} and str(delivery.state) == "settled":
                return _admission(
                    {
                        "kind": "admission_noncompletion",
                        **base,
                        "run_id": run_id,
                        "binding": "bound" if mission.run_id else "unbound",
                        "reason": "retention_failed"
                        if str(run.status) == "failed" and (run.error or "").startswith("admission failed:")
                        else "run_terminal",
                    }
                )
            if str(run.status) not in {"queued", "running"} or str(delivery.state) not in {
                "pending",
                "published",
                "claimed",
            }:
                msg = "Canonical delivery has not been released for execution."
                raise PlacementUnavailableError(msg)  # noqa: TRY301 - held or incoherent admission is unresolved
            if mission.run_id != run_id:
                msg = "Canonical Run is not bound."
                raise PlacementUnavailableError(msg)  # noqa: TRY301 - translate unreadable admission to closed unresolved result
            return _admission({"kind": "admitted", **base, "run_id": run_id})
        except PlacementUnavailableError:
            return _admission(
                {"kind": "admission_unresolved", **base, "run_id": run_id, "reason": "custody_unavailable"}
            )
