"""Retain exact native Consent binding identity.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["downgrade", "upgrade"]

revision = "0010"
down_revision = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Add requiredness and identity without rewriting historical verdicts."""
    op.add_column(
        "consent", sa.Column("native_review_required", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("consent", sa.Column("native_binding", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_consent_native_binding_requirement"),
        "consent",
        "(NOT native_review_required AND native_binding IS NULL) OR "
        "(native_review_required AND native_binding IS NOT NULL AND jsonb_typeof(native_binding) = 'object')",
    )


def downgrade() -> None:
    """Refuse to erase native identity, including settled history, under one lock."""
    op.execute(sa.text("LOCK TABLE consent IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM consent WHERE native_review_required OR native_binding IS NOT NULL LIMIT 1) THEN
                RAISE EXCEPTION 'LychD migration 0010 downgrade requires no native Consent bindings';
            END IF;
        END
        $$;
    """)
    )
    op.drop_constraint(op.f("ck_consent_native_binding_requirement"), "consent", type_="check")
    op.drop_column("consent", "native_binding")
    op.drop_column("consent", "native_review_required")
