"""PostgreSQL transactions for bounded placement custody, claims and receipts.

Every mutation locks mission, action, Run, Consent and exact policy in that order.
No transaction spans an effect; committed claim identity never recreates a grant.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lychd.compositions.blockworld.ports import (
    ActionSnapshot,
    ExistingDisposition,
    MissionSnapshot,
    NewClaim,
    PlacementAuthorityPort,
    PreparedAction,
    Refused,
)
from lychd.compositions.blockworld.review import prepare_binding
from lychd.compositions.blockworld.schemas import (
    AdmittedPlacementV1,
    PlacementCommitUnknownError,
    PlacementConflictError,
    PlacementEvidence,
    PlacementIntegrityError,
    PlacementNotFoundError,
    PlacementResult,
    PlacementUnavailableError,
    RefusalCode,
    UnverifiedEffectResult,
    canonical_bytes,
    load_document,
    receipt_digest,
)
from lychd.compositions.blockworld.services import (
    authority_refusal,
    consent_refusal,
    exact_policy_eligible,
    not_issued,
    prepare_command,
    receipt_for,
    result_for,
    validate_action,
    validate_admitted_run,
    validate_mission,
)
from lychd.db.models.blockworld import BlockworldPlacementAction, BlockworldPlacementMission
from lychd.db.models.codex import CodexPreauthorization
from lychd.db.models.consent import Consent
from lychd.db.models.run import Run
from lychd.domain.codex.schemas import NativeConsentBinding, decode_native_binding
from lychd.domain.codex.services import (
    PREAUTH_DIGEST_PAYLOAD_KEY,
    ConsentService,
    PreauthService,
    preauth_authorization_digest,
    row_to_view,
)
from lychd.domain.codex.sigil import Sigil


def _decode_mission(row: BlockworldPlacementMission) -> MissionSnapshot:
    if (
        sha256(row.admission_bytes).hexdigest() != row.admission_digest
        or sha256(row.request_bytes).hexdigest() != row.request_digest
    ):
        msg = "Mission admission bytes differ from their digest."
        raise PlacementIntegrityError(msg)
    admission = AdmittedPlacementV1.model_validate_json(row.admission_bytes)
    if (
        canonical_bytes(admission) != row.admission_bytes
        or canonical_bytes(admission.request) != row.request_bytes
        or str(row.id) != admission.request.mission_id
        or str(row.request_id) != admission.request.request_id
        or str(row.action_id) != admission.request.action_id
        or row.pattern_snapshot != admission.model_dump(mode="json")["pattern_snapshot"]
        or row.pattern_digest != admission.pattern_snapshot["digest"]
    ):
        msg = "Mission identity or Pattern differs from frozen admission."
        raise PlacementIntegrityError(msg)
    result = (
        TypeAdapter(PlacementResult).validate_python(load_document(row.result_bytes))
        if row.result_bytes is not None
        else None
    )
    if result is not None and canonical_bytes(result) != row.result_bytes:
        msg = "Retained mission result bytes are not canonical."
        raise PlacementIntegrityError(msg)
    mission = MissionSnapshot(
        admission=admission,
        run_id=str(row.run_id) if row.run_id else None,
        cursor=row.cursor,
        placements_remaining=row.placements_remaining,
        pending_action_id=str(row.pending_action_id) if row.pending_action_id else None,
        result=result,
    )
    validate_mission(mission)
    return mission


def _mission(row: BlockworldPlacementMission) -> MissionSnapshot:
    try:
        return _decode_mission(row)
    except (ValueError, TypeError) as exc:
        msg = "Retained mission document is corrupt."
        raise PlacementIntegrityError(msg) from exc


def _action(row: BlockworldPlacementAction) -> ActionSnapshot:
    binding = decode_native_binding(row.native_binding)
    if binding is None or binding.digest != row.native_binding_digest:
        msg = "Action binding is corrupt."
        raise PlacementIntegrityError(msg)
    return ActionSnapshot(
        action_id=str(row.id),
        mission_id=str(row.mission_id),
        run_id=str(row.run_id),
        consent_id=str(row.consent_id),
        command_bytes=row.command_bytes,
        preparation_digest=row.preparation_digest,
        review_bytes=row.review_bytes,
        native_binding=binding,
        status=cast("Any", row.status),
        claim_id=str(row.claim_id) if row.claim_id else None,
        issuance_judgment=row.issuance_judgment,
        claimed_at=row.claimed_at,
        reason=row.reason,
        receipt_bytes=row.receipt_bytes,
        receipt_digest=row.receipt_digest,
    )


async def _commit(session: AsyncSession) -> None:
    try:
        await session.commit()
    except Exception as exc:
        with contextlib.suppress(Exception):
            await session.rollback()
        msg = "Placement commit acknowledgement is unavailable."
        raise PlacementCommitUnknownError(msg) from exc


class DbPlacementStore:
    """Own the two placement tables without any normal-boot registration."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], authority: PlacementAuthorityPort) -> None:
        """Bind fresh transaction handles and read-only external authority."""
        self._session_factory, self._authority = session_factory, authority

    async def _locked_mission(
        self,
        session: AsyncSession,
        *,
        run_id: str | None = None,
        request_id: str | None = None,
        mission_id: UUID | None = None,
    ) -> BlockworldPlacementMission:
        query = select(BlockworldPlacementMission)
        if run_id is not None:
            query = query.where(BlockworldPlacementMission.run_id == UUID(run_id))
        elif request_id is not None:
            query = query.where(BlockworldPlacementMission.request_id == UUID(request_id))
        else:
            query = query.where(BlockworldPlacementMission.id == mission_id)
        row = await session.scalar(query.with_for_update())
        if row is None:
            msg = "Retained mission is unavailable."
            raise PlacementUnavailableError(msg)
        return row

    async def _locked_pair(
        self, session: AsyncSession, action_id: str
    ) -> tuple[BlockworldPlacementMission, BlockworldPlacementAction]:
        mission_id = await session.scalar(
            select(BlockworldPlacementAction.mission_id).where(BlockworldPlacementAction.id == UUID(action_id))
        )
        if mission_id is None:
            msg = "Retained action is unavailable."
            raise PlacementNotFoundError(msg)
        mission = await self._locked_mission(session, mission_id=mission_id)
        action = await session.scalar(
            select(BlockworldPlacementAction).where(BlockworldPlacementAction.id == UUID(action_id)).with_for_update()
        )
        if action is None:
            msg = "Locked action disappeared."
            raise PlacementIntegrityError(msg)
        validate_action(_action(action), _mission(mission))
        return mission, action

    async def read_mission(self, request_id: str) -> MissionSnapshot:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(BlockworldPlacementMission).where(BlockworldPlacementMission.request_id == UUID(request_id))
            )
            if row is None:
                msg = "Retained mission is unavailable."
                raise PlacementUnavailableError(msg)
            return _mission(row)

    async def load_for_run(self, run_id: str) -> MissionSnapshot:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(BlockworldPlacementMission).where(BlockworldPlacementMission.run_id == UUID(run_id))
            )
            if row is None:
                msg = "Retained mission is unavailable."
                raise PlacementUnavailableError(msg)
            return _mission(row)

    async def read_action(self, action_id: str) -> ActionSnapshot:
        async with self._session_factory() as session:
            row = await session.get(BlockworldPlacementAction, UUID(action_id))
            if row is None:
                msg = "Retained action is unavailable."
                raise PlacementNotFoundError(msg)
            mission = await session.get(BlockworldPlacementMission, row.mission_id)
            if mission is None:
                msg = "Action mission is missing."
                raise PlacementIntegrityError(msg)
            action = _action(row)
            validate_action(action, _mission(mission))
            return action

    async def retain_run(self, request_id: str, canonical_run_id: str) -> None:
        async with self._session_factory() as session:
            row = await self._locked_mission(session, request_id=request_id)
            mission = _mission(row)
            if mission.run_id not in (None, canonical_run_id):
                msg = "Mission already binds another Run."
                raise PlacementConflictError(msg)
            if mission.result is not None:
                msg = "Terminal mission cannot be rebound."
                raise PlacementConflictError(msg)
            if row.run_id is None:
                row.run_id = UUID(canonical_run_id)
                await session.flush()
                await _commit(session)

    async def prepare_and_admit(self, run_id: str) -> PreparedAction | ExistingDisposition | Refused:  # noqa: C901, PLR0911 - one atomic preparation transaction
        async with self._session_factory() as session:
            mission_row = await self._locked_mission(session, run_id=run_id)
            mission = _mission(mission_row)
            action_row = await session.scalar(
                select(BlockworldPlacementAction)
                .where(BlockworldPlacementAction.id == mission_row.action_id)
                .with_for_update()
            )
            if action_row is not None:
                action = _action(action_row)
                validate_action(action, mission)
                if action.status == "prepared":
                    return PreparedAction(
                        action_id=action.action_id,
                        consent_id=action.consent_id,
                        preparation_digest=action.preparation_digest,
                        native_binding=action.native_binding,
                        status=action.status,
                    )
                return ExistingDisposition(action_id=action.action_id, status=cast("Any", action.status))
            if mission.result is not None:
                if mission.result.kind != "not_issued":
                    msg = "Mission-only result cannot describe an effect."
                    raise PlacementIntegrityError(msg)
                return Refused(result=mission.result)
            run = await session.scalar(select(Run).where(Run.id == UUID(run_id)).with_for_update())
            if run is None:
                return await self._refuse_mission(session, mission_row, "required_record_missing")
            validate_admitted_run(mission, run)
            if run.status in {"cancelling", "cancelled", "failed", "done"}:
                return await self._refuse_mission(session, mission_row, "consent_cancelled")
            command = prepare_command(mission.admission, run_id)
            command_bytes = canonical_bytes(command)
            binding, review = prepare_binding(command, mission.admission)
            principal = Sigil(name=command.principal.name, scopes=frozenset(command.principal.scopes))
            facts = await self._authority.current(command_bytes, principal)
            now = cast("datetime", await session.scalar(select(func.clock_timestamp())))
            refusal = authority_refusal(command, facts, mission, now, principal)
            if refusal is not None:
                return await self._refuse_mission(session, mission_row, refusal)
            policy = None
            admission = mission.admission
            if admission.judgment_mode != "live":
                selected = await session.scalar(
                    select(CodexPreauthorization)
                    .where(CodexPreauthorization.id == UUID(admission.policy_id))
                    .with_for_update()
                )
                now = cast("datetime", await session.scalar(select(func.clock_timestamp())))
                if exact_policy_eligible(selected, command, now):
                    policy = await PreauthService(session=session).match_and_consume(
                        sigil=principal,
                        tool_name="blockworld.place_block",
                        payload={
                            "action_id": command.action_id,
                            "mission_id": command.mission_id,
                            "preparation_digest": binding.preparation_digest,
                        },
                        policy_id=UUID(admission.policy_id),
                        auto_commit=False,
                    )
                if policy is None and admission.judgment_mode == "exact_preauth":
                    return await self._refuse_mission(session, mission_row, "policy_changed")
            consent = await ConsentService(session=session).request(
                run_id=run_id,
                tool_name="blockworld.place_block",
                tool_call_id=command.action_id,
                call_ids=(command.action_id,),
                payload={
                    "action_id": command.action_id,
                    "mission_id": command.mission_id,
                    "preparation_digest": binding.preparation_digest,
                },
                preauth=policy,
                native_binding=binding,
                auto_commit=False,
            )
            action_row = BlockworldPlacementAction(
                id=UUID(command.action_id),
                mission_id=mission_row.id,
                run_id=UUID(run_id),
                consent_id=consent.id,
                expected_cursor=0,
                command_bytes=command_bytes,
                preparation_digest=binding.preparation_digest,
                review_bytes=review.canonical_bytes(),
                native_binding=binding.document(),
                native_binding_digest=binding.digest,
                status="prepared",
            )
            session.add(action_row)
            await session.flush()
            result = PreparedAction(
                action_id=command.action_id,
                consent_id=str(consent.id),
                preparation_digest=binding.preparation_digest,
                native_binding=binding,
                status="prepared",
            )
            try:
                await _commit(session)
            except PlacementCommitUnknownError:
                # A fresh handle confirms exact custody; it never repeats consumption.
                action = await self.read_action(command.action_id)
                if action.command_bytes != command_bytes or action.native_binding != binding:
                    msg = "Uncertain preparation resolved to different custody."
                    raise PlacementIntegrityError(msg) from None
                return PreparedAction(
                    action_id=action.action_id,
                    consent_id=action.consent_id,
                    preparation_digest=action.preparation_digest,
                    native_binding=action.native_binding,
                    status=action.status,
                )
            return result

    async def _refuse_mission(
        self, session: AsyncSession, row: BlockworldPlacementMission, reason: RefusalCode
    ) -> Refused:
        request_id = str(row.request_id)
        result = not_issued(str(row.action_id), reason)
        row.result_bytes = canonical_bytes(result)
        await session.flush()
        try:
            await _commit(session)
        except PlacementCommitUnknownError:
            current = await self.read_mission(request_id)
            if current.result != result:
                raise
        return Refused(result=result)

    async def _forward(
        self,
        session: AsyncSession,
        mission_row: BlockworldPlacementMission,
        action_row: BlockworldPlacementAction,
        principal: Sigil,
    ) -> tuple[RefusalCode | None, dict[str, Any], Any]:
        mission, action = _mission(mission_row), _action(action_row)
        command = validate_action(action, mission)
        run = await session.scalar(select(Run).where(Run.id == action_row.run_id).with_for_update())
        consent = await session.scalar(select(Consent).where(Consent.id == action_row.consent_id).with_for_update())
        policy = None
        if consent is not None and consent.preauth_slug is not None and mission.admission.policy_id is not None:
            policy = await session.scalar(
                select(CodexPreauthorization)
                .where(CodexPreauthorization.id == UUID(mission.admission.policy_id))
                .with_for_update()
            )
        if run is not None:
            validate_admitted_run(mission, run)
        facts = await self._authority.current(action.command_bytes, principal)
        # clock_timestamp, unlike transaction-stable now(), is fresh after external awaits.
        now = cast("datetime", await session.scalar(select(func.clock_timestamp())))
        refusal = consent_refusal(
            action,
            row_to_view(consent) if consent else None,
            run,
            policy,
            now,
            authorization_digest=consent.payload.get(PREAUTH_DIGEST_PAYLOAD_KEY) if consent else None,
        )
        if refusal is None:
            refusal = authority_refusal(command, facts, mission, now, principal)
        if refusal is None and mission.pending_action_id not in (None, action.action_id):
            refusal = "cursor_changed"
        judgment = (
            {}
            if consent is None
            else {
                "consent_id": str(consent.id),
                "binding_digest": action.native_binding.digest,
                "verdict": consent.status,
                "decided_by": consent.decided_by,
                "decided_at": consent.decided_at.isoformat().replace("+00:00", "Z") if consent.decided_at else None,
                "policy_id": str(policy.id) if policy else None,
                "policy_slug": policy.slug if policy else None,
                "policy_digest": preauth_authorization_digest(policy) if policy else None,
            }
        )
        return refusal, judgment, now

    async def claim_issue(
        self, action_id: str, expected_binding: NativeConsentBinding, principal: Sigil
    ) -> NewClaim | ExistingDisposition | Refused:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, action_id)
            if action.status != "prepared":
                return ExistingDisposition(action_id=action_id, status=cast("Any", action.status))
            if _action(action).native_binding != expected_binding:
                msg = "Claim binding differs from retained action."
                raise PlacementIntegrityError(msg)
            refusal, judgment, now = await self._forward(session, mission, action, principal)
            if refusal is not None:
                return await self._write_refusal(session, mission, action, refusal)
            if mission.pending_action_id is not None:
                return await self._write_refusal(session, mission, action, "cursor_changed")
            claim_id = uuid4()
            action.status, action.claim_id, action.issuance_judgment, action.claimed_at = (
                "pending",
                claim_id,
                judgment,
                now,
            )
            mission.pending_action_id = action.id
            await session.flush()
            command_bytes = action.command_bytes
            await _commit(session)
            return NewClaim(action_id=action_id, claim_id=str(claim_id), command_bytes=command_bytes)

    async def validate_live_claim_before_invoke(self, claim: NewClaim, principal: Sigil) -> Any:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, claim.action_id)
            if action.status != "pending" or str(action.claim_id) != claim.claim_id:
                return ExistingDisposition(action_id=claim.action_id, status=cast("Any", action.status))
            if claim.command_bytes != action.command_bytes:
                msg = "Live claim command differs from custody."
                raise PlacementIntegrityError(msg)
            refusal, judgment, _ = await self._forward(session, mission, action, principal)
            if refusal is None and (mission.pending_action_id != action.id or judgment != action.issuance_judgment):
                refusal = "policy_changed"
            if refusal is not None:
                return await self._write_refusal(session, mission, action, refusal)
            await _commit(session)
            return "ready"

    async def _write_refusal(
        self,
        session: AsyncSession,
        mission: BlockworldPlacementMission,
        action: BlockworldPlacementAction,
        reason: RefusalCode,
    ) -> Refused:
        result = not_issued(str(action.id), reason)
        action.status, action.reason = "not_issued", reason
        mission.pending_action_id, mission.result_bytes = None, canonical_bytes(result)
        await session.flush()
        await _commit(session)
        return Refused(result=result)

    async def record_not_issued_before_invoke(
        self, action_id: str, claim_id: str, reason: RefusalCode
    ) -> ExistingDisposition | Refused:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, action_id)
            if action.status != "pending" or str(action.claim_id) != claim_id:
                return ExistingDisposition(action_id=action_id, status=cast("Any", action.status))
            return await self._write_refusal(session, mission, action, reason)

    async def refuse_prepared(self, action_id: str, reason: RefusalCode) -> ExistingDisposition | Refused:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, action_id)
            if action.status != "prepared":
                return ExistingDisposition(action_id=action_id, status=cast("Any", action.status))
            return await self._write_refusal(session, mission, action, reason)

    async def record_unknown(self, action_id: str, reason: str) -> PlacementResult:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, action_id)
            if action.status in {"verified", "not_issued", "unverified_effect"}:
                return result_for(_action(action), _mission(mission))
            if action.status != "pending":
                msg = "Unknown observation has no committed claim."
                raise PlacementIntegrityError(msg)
            result = UnverifiedEffectResult(
                kind="unverified_effect",
                action_id=action_id,
                reason=cast("Any", reason),
                cursor=0,
                placements_remaining=1,
            )
            action.status, action.reason = "unverified_effect", reason
            mission.result_bytes = canonical_bytes(result)
            await session.flush()
            await _commit(session)
            return result

    async def settle_verified(self, action_id: str, evidence: PlacementEvidence) -> PlacementResult:
        async with self._session_factory() as session:
            mission, action = await self._locked_pair(session, action_id)
            command = validate_action(_action(action), _mission(mission))
            if action.status == "verified":
                from lychd.compositions.blockworld.schemas import PlacementReceiptV1

                retained = PlacementReceiptV1.model_validate_json(action.receipt_bytes)
                receipt = receipt_for(command, evidence, retained.receipt_id)
                if canonical_bytes(receipt) != action.receipt_bytes:
                    msg = "Conflicting evidence for a retained receipt."
                    raise PlacementIntegrityError(msg)
                return result_for(_action(action), _mission(mission))
            if action.status not in {"pending", "unverified_effect"}:
                msg = "Effect evidence conflicts with non-issuance custody."
                raise PlacementIntegrityError(msg)
            receipt = receipt_for(command, evidence, str(uuid4()))
            encoded = canonical_bytes(receipt)
            action.status, action.reason, action.receipt_bytes, action.receipt_digest = (
                "verified",
                None,
                encoded,
                receipt_digest(encoded),
            )
            mission.cursor, mission.placements_remaining, mission.pending_action_id, mission.result_bytes = (
                1,
                0,
                None,
                None,
            )
            result = result_for(_action(action), _mission(mission))
            mission.result_bytes = canonical_bytes(result)
            await session.flush()
            await _commit(session)
            return result
