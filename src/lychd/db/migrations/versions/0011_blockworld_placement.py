"""Retain bounded Blockworld placement custody independently of Core.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _audit() -> list[sa.Column[UUID] | sa.Column[int] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "blockworld_placement_mission",
        *_audit(),
        sa.Column("request_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("action_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("run_id", sa.UUID(), nullable=True, unique=True),
        sa.Column("admission_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("admission_digest", sa.String(64), nullable=False),
        sa.Column("request_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("pattern_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("pattern_digest", sa.String(64), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("placements_remaining", sa.Integer(), nullable=False),
        sa.Column("pending_action_id", sa.UUID(), nullable=True),
        sa.Column("result_bytes", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "cursor IN (0, 1) AND placements_remaining IN (0, 1) AND cursor + placements_remaining = 1",
            name=op.f("ck_blockworld_placement_mission_placement_limits"),
        ),
        sa.CheckConstraint(
            "pending_action_id IS NULL OR pending_action_id = action_id",
            name=op.f("ck_blockworld_placement_mission_pending_owner"),
        ),
    )
    op.create_table(
        "blockworld_placement_action",
        *_audit(),
        sa.Column(
            "mission_id",
            sa.UUID(),
            sa.ForeignKey("blockworld_placement_mission.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("consent_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("expected_cursor", sa.Integer(), nullable=False),
        sa.Column("command_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("preparation_digest", sa.String(64), nullable=False),
        sa.Column("review_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("native_binding", postgresql.JSONB(), nullable=False),
        sa.Column("native_binding_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("issuance_judgment", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("receipt_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("receipt_digest", sa.String(64), nullable=True),
        sa.UniqueConstraint("mission_id", "expected_cursor", name="uq_blockworld_placement_action_mission_cursor"),
        sa.CheckConstraint("expected_cursor = 0", name=op.f("ck_blockworld_placement_action_expected_cursor")),
        sa.CheckConstraint(
            "status IN ('prepared','pending','not_issued','unverified_effect','verified')",
            name=op.f("ck_blockworld_placement_action_status"),
        ),
        sa.CheckConstraint(
            "(claim_id IS NULL AND issuance_judgment IS NULL AND claimed_at IS NULL) OR (claim_id IS NOT NULL AND issuance_judgment IS NOT NULL AND claimed_at IS NOT NULL)",
            name=op.f("ck_blockworld_placement_action_claim_complete"),
        ),
        sa.CheckConstraint(
            "status <> 'prepared' OR claim_id IS NULL", name=op.f("ck_blockworld_placement_action_prepared_unclaimed")
        ),
        sa.CheckConstraint(
            "status NOT IN ('pending','unverified_effect','verified') OR claim_id IS NOT NULL",
            name=op.f("ck_blockworld_placement_action_issued_claimed"),
        ),
        sa.CheckConstraint(
            "(status = 'verified' AND receipt_bytes IS NOT NULL AND receipt_digest IS NOT NULL) OR (status <> 'verified' AND receipt_bytes IS NULL AND receipt_digest IS NULL)",
            name=op.f("ck_blockworld_placement_action_receipt_complete"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(native_binding) = 'object' AND native_binding ? 'review_digest' AND native_binding ? 'preparation_digest'",
            name=op.f("ck_blockworld_placement_action_native_binding_complete"),
        ),
    )


def downgrade() -> None:
    """Never erase retained history; locks exclude concurrent fixture insertion."""
    op.execute(sa.text("LOCK TABLE blockworld_placement_mission IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("LOCK TABLE blockworld_placement_action IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM blockworld_placement_mission LIMIT 1)
         OR EXISTS (SELECT 1 FROM blockworld_placement_action LIMIT 1) THEN
        RAISE EXCEPTION 'Blockworld placement custody requires explicit retirement before downgrade';
      END IF;
    END $$;""")
    )
    op.drop_table("blockworld_placement_action")
    op.drop_table("blockworld_placement_mission")
