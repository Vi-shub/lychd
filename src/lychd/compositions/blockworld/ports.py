"""Bounded custody and external-fact ports; no ambient registration or effects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol

from lychd.compositions.blockworld.schemas import (
    ActionStatus,
    AdmittedPlacementV1,
    CurrentPlacementFacts,
    NotIssuedResult,
    PlacementEvidence,
    PlacementResult,
    RefusalCode,
    ReviewEligibility,
    UnknownEvidence,
)
from lychd.domain.codex.schemas import NativeConsentBinding
from lychd.domain.codex.sigil import Sigil


@dataclass(frozen=True, kw_only=True)
class MissionSnapshot:
    admission: AdmittedPlacementV1
    run_id: str | None
    cursor: int = 0
    placements_remaining: int = 1
    pending_action_id: str | None = None
    result: PlacementResult | None = None


@dataclass(frozen=True, kw_only=True)
class ActionSnapshot:
    action_id: str
    mission_id: str
    run_id: str
    consent_id: str
    command_bytes: bytes
    preparation_digest: str
    review_bytes: bytes
    native_binding: NativeConsentBinding
    status: ActionStatus
    claim_id: str | None = None
    issuance_judgment: Mapping[str, Any] | None = None
    claimed_at: datetime | None = None
    reason: str | None = None
    receipt_bytes: bytes | None = None
    receipt_digest: str | None = None

    def __post_init__(self) -> None:
        """Copy retained scalar judgment fields into immutable snapshot custody."""
        if self.issuance_judgment is not None:
            object.__setattr__(self, "issuance_judgment", MappingProxyType(dict(self.issuance_judgment)))


@dataclass(frozen=True, kw_only=True)
class PreparedAction:
    action_id: str
    consent_id: str
    preparation_digest: str
    native_binding: NativeConsentBinding
    status: ActionStatus


@dataclass(frozen=True, kw_only=True)
class NewClaim:
    action_id: str
    claim_id: str
    command_bytes: bytes
    kind: Literal["new_claim"] = "new_claim"


@dataclass(frozen=True, kw_only=True)
class ExistingDisposition:
    action_id: str
    status: ActionStatus
    kind: Literal["existing"] = "existing"


@dataclass(frozen=True, kw_only=True)
class Refused:
    result: NotIssuedResult
    kind: Literal["refused"] = "refused"


class PlacementAuthorityPort(Protocol):
    async def current(self, command_bytes: bytes, principal: Sigil) -> CurrentPlacementFacts: ...
    async def review_access(
        self, action_id: str, audience_digest: str, reviewer: Sigil, purpose: Literal["view", "decide"]
    ) -> ReviewEligibility: ...


class PlacementEffectPort(Protocol):
    async def invoke(self, command_bytes: bytes) -> PlacementEvidence | UnknownEvidence: ...
    async def status(self, command_bytes: bytes) -> PlacementEvidence | UnknownEvidence: ...


class PlacementStore(Protocol):
    async def read_mission(self, request_id: str) -> MissionSnapshot: ...
    async def retain_run(self, request_id: str, canonical_run_id: str) -> None: ...
    async def load_for_run(self, run_id: str) -> MissionSnapshot: ...
    async def prepare_and_admit(self, run_id: str) -> PreparedAction | ExistingDisposition | Refused: ...
    async def read_action(self, action_id: str) -> ActionSnapshot: ...
    async def claim_issue(
        self, action_id: str, expected_binding: NativeConsentBinding, principal: Sigil
    ) -> NewClaim | ExistingDisposition | Refused: ...
    async def validate_live_claim_before_invoke(
        self, claim: NewClaim, principal: Sigil
    ) -> Literal["ready"] | ExistingDisposition | Refused: ...
    async def record_not_issued_before_invoke(
        self, action_id: str, claim_id: str, reason: RefusalCode
    ) -> ExistingDisposition | Refused: ...
    async def refuse_prepared(self, action_id: str, reason: RefusalCode) -> ExistingDisposition | Refused: ...
    async def record_unknown(self, action_id: str, reason: str) -> PlacementResult: ...
    async def settle_verified(self, action_id: str, evidence: PlacementEvidence) -> PlacementResult: ...
