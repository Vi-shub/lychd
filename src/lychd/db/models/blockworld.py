"""Independent application custody for one admitted Blockworld placement."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class BlockworldPlacementMission(UUIDAuditBase):
    __tablename__ = "blockworld_placement_mission"
    __table_args__ = (
        CheckConstraint(
            "cursor IN (0, 1) AND placements_remaining IN (0, 1) AND cursor + placements_remaining = 1",
            name="placement_limits",
        ),
        CheckConstraint("pending_action_id IS NULL OR pending_action_id = action_id", name="pending_owner"),
    )
    request_id: Mapped[UUID] = mapped_column(unique=True)
    action_id: Mapped[UUID] = mapped_column(unique=True)
    run_id: Mapped[UUID | None] = mapped_column(unique=True, nullable=True)
    admission_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    admission_digest: Mapped[str] = mapped_column(String(64))
    request_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    request_digest: Mapped[str] = mapped_column(String(64))
    pattern_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    pattern_digest: Mapped[str] = mapped_column(String(64))
    cursor: Mapped[int] = mapped_column(default=0)
    placements_remaining: Mapped[int] = mapped_column(default=1)
    pending_action_id: Mapped[UUID | None] = mapped_column(nullable=True)
    result_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)


class BlockworldPlacementAction(UUIDAuditBase):
    __tablename__ = "blockworld_placement_action"
    __table_args__ = (
        UniqueConstraint("mission_id", "expected_cursor", name="uq_blockworld_placement_action_mission_cursor"),
        CheckConstraint("expected_cursor = 0", name="expected_cursor"),
        CheckConstraint("status IN ('prepared','pending','not_issued','unverified_effect','verified')", name="status"),
        CheckConstraint(
            "(claim_id IS NULL AND issuance_judgment IS NULL AND claimed_at IS NULL) OR (claim_id IS NOT NULL AND issuance_judgment IS NOT NULL AND claimed_at IS NOT NULL)",
            name="claim_complete",
        ),
        CheckConstraint("status <> 'prepared' OR claim_id IS NULL", name="prepared_unclaimed"),
        CheckConstraint(
            "status NOT IN ('pending','unverified_effect','verified') OR claim_id IS NOT NULL", name="issued_claimed"
        ),
        CheckConstraint(
            "(status = 'verified' AND receipt_bytes IS NOT NULL AND receipt_digest IS NOT NULL) OR (status <> 'verified' AND receipt_bytes IS NULL AND receipt_digest IS NULL)",
            name="receipt_complete",
        ),
        CheckConstraint(
            "jsonb_typeof(native_binding) = 'object' AND native_binding ? 'review_digest' AND native_binding ? 'preparation_digest'",
            name="native_binding_complete",
        ),
    )
    mission_id: Mapped[UUID] = mapped_column(ForeignKey("blockworld_placement_mission.id", ondelete="RESTRICT"))
    run_id: Mapped[UUID]
    consent_id: Mapped[UUID] = mapped_column(unique=True)
    expected_cursor: Mapped[int] = mapped_column(default=0)
    command_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    preparation_digest: Mapped[str] = mapped_column(String(64))
    review_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    native_binding: Mapped[dict[str, Any]] = mapped_column(JSONB)
    native_binding_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    claim_id: Mapped[UUID | None] = mapped_column(nullable=True)
    issuance_judgment: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    receipt_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
