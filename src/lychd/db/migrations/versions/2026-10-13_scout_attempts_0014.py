"""Retain exact Scout attempts, native judgments and nonrefundable uncertainty.

Revision ID: 0014
Revises: 0013

Schema operations were derived by Alembic from the admitted Scout models.
Transactional entry points and locked downgrade refusal preserve retained custody.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create both custody tables within Alembic's PostgreSQL transaction."""
    op.create_table(
        "scout_budget",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("configuration_digest", sa.String(length=64), nullable=False),
        sa.Column("active_claims", sa.Integer(), nullable=False),
        sa.Column("connects_limit", sa.BigInteger(), nullable=False),
        sa.Column("transmissions_limit", sa.BigInteger(), nullable=False),
        sa.Column("wire_bytes_limit", sa.BigInteger(), nullable=False),
        sa.Column("connects_reserved", sa.BigInteger(), nullable=False),
        sa.Column("transmissions_reserved", sa.BigInteger(), nullable=False),
        sa.Column("wire_bytes_reserved", sa.BigInteger(), nullable=False),
        sa.Column("connects_used", sa.BigInteger(), nullable=False),
        sa.Column("transmissions_used", sa.BigInteger(), nullable=False),
        sa.Column("wire_bytes_used", sa.BigInteger(), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'host' AND active_claims = 0) OR (scope = 'binding' AND active_claims BETWEEN 0 AND 1)",
            name=op.f("ck_scout_budget_active_binding_limit"),
        ),
        sa.CheckConstraint("scope IN ('host','binding')", name=op.f("ck_scout_budget_scope")),
        sa.CheckConstraint(
            "connects_limit >= 0 AND connects_reserved >= 0 AND connects_used >= 0 AND connects_used <= connects_limit - connects_reserved",
            name=op.f("ck_scout_budget_connects_ceiling"),
        ),
        sa.CheckConstraint(
            "transmissions_limit >= 0 AND transmissions_reserved >= 0 AND transmissions_used >= 0 AND transmissions_used <= transmissions_limit - transmissions_reserved",
            name=op.f("ck_scout_budget_transmissions_ceiling"),
        ),
        sa.CheckConstraint(
            "wire_bytes_limit >= 0 AND wire_bytes_reserved >= 0 AND wire_bytes_used >= 0 AND wire_bytes_used <= wire_bytes_limit - wire_bytes_reserved",
            name=op.f("ck_scout_budget_wire_bytes_ceiling"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scout_budget")),
        sa.UniqueConstraint("scope", name=op.f("uq_scout_budget_scope")),
    )
    op.create_table(
        "scout_attempt",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("request_id", GUID(length=16), nullable=False),
        sa.Column("run_id", GUID(length=16), nullable=False),
        sa.Column("consent_id", GUID(length=16), nullable=False),
        sa.Column("preparation_enqueue_seq", sa.BigInteger(), nullable=False),
        sa.Column("preparation_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("preparation_digest", sa.String(length=64), nullable=False),
        sa.Column("native_binding", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("review_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("claim_id", GUID(length=16), nullable=True),
        sa.Column("execution_enqueue_seq", sa.BigInteger(), nullable=True),
        sa.Column("claimed_at", DateTimeUTC(timezone=True), nullable=True),
        sa.Column("receipt_id", GUID(length=16), nullable=True),
        sa.Column("settlement_bytes", sa.LargeBinary(), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'prepared' AND claim_id IS NULL AND execution_enqueue_seq IS NULL AND claimed_at IS NULL) OR (status <> 'prepared' AND claim_id IS NOT NULL AND execution_enqueue_seq IS NOT NULL AND claimed_at IS NOT NULL)",
            name=op.f("ck_scout_attempt_claim_complete"),
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','refused') AND receipt_id IS NOT NULL AND settlement_bytes IS NOT NULL) OR (status NOT IN ('succeeded','refused') AND receipt_id IS NULL AND settlement_bytes IS NULL)",
            name=op.f("ck_scout_attempt_settlement_complete"),
        ),
        sa.CheckConstraint(
            "status IN ('prepared','claimed','unknown_after_crash','succeeded','refused')",
            name=op.f("ck_scout_attempt_status"),
        ),
        sa.CheckConstraint(
            "execution_enqueue_seq IS NULL OR execution_enqueue_seq >= 0",
            name=op.f("ck_scout_attempt_execution_sequence"),
        ),
        sa.CheckConstraint("preparation_enqueue_seq >= 0", name=op.f("ck_scout_attempt_preparation_sequence")),
        sa.ForeignKeyConstraint(
            ["consent_id"], ["consent.id"], name=op.f("fk_scout_attempt_consent_id_consent"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["run.id"], name=op.f("fk_scout_attempt_run_id_run"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scout_attempt")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_scout_attempt_claim_id")),
        sa.UniqueConstraint("consent_id", name=op.f("uq_scout_attempt_consent_id")),
        sa.UniqueConstraint("receipt_id", name=op.f("uq_scout_attempt_receipt_id")),
        sa.UniqueConstraint("request_id", name=op.f("uq_scout_attempt_request_id")),
    )


def downgrade() -> None:
    """Refuse retained history loss while holding locks against concurrent writes."""
    op.execute(sa.text("LOCK TABLE scout_budget IN ACCESS EXCLUSIVE MODE"))
    op.execute(sa.text("LOCK TABLE scout_attempt IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM scout_budget LIMIT 1)
         OR EXISTS (SELECT 1 FROM scout_attempt LIMIT 1) THEN
        RAISE EXCEPTION 'Retained Scout custody prevents downgrade';
      END IF;
    END $$;""")
    )
    op.drop_table("scout_attempt")
    op.drop_table("scout_budget")
