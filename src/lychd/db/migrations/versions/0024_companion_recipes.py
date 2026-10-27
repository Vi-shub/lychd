"""Retain bounded private companion command aliases in PostgreSQL.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-27

Schema operations mirror CompanionRecipe metadata, with offline column and
constraint parity checks. No live migration or PostgreSQL qualification is
claimed. Downgrade never deletes retained aliases implicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["downgrade", "upgrade"]

revision = "0024"
down_revision = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    """Create private alias custody without importing or transforming old data."""
    op.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    op.create_table(
        "companion_recipe",
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("phrase", sa.String(length=200), nullable=False),
        sa.Column("recipe_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("recipe_digest", sa.String(length=64), nullable=False),
        sa.Column("learned_request_id", GUID(length=16), nullable=False),
        sa.Column("learned_run_id", GUID(length=16), nullable=False),
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.CheckConstraint("namespace ~ '^[0-9a-f]{64}$'", name=op.f("ck_companion_recipe_namespace_sha256")),
        sa.CheckConstraint("key ~ '^[0-9a-f]{64}$'", name=op.f("ck_companion_recipe_key_sha256")),
        sa.CheckConstraint("language IN ('sk', 'cs', 'en')", name=op.f("ck_companion_recipe_language_closed")),
        sa.CheckConstraint(
            "char_length(phrase) BETWEEN 1 AND 200 AND octet_length(phrase) BETWEEN 1 AND 800",
            name=op.f("ck_companion_recipe_phrase_bound"),
        ),
        sa.CheckConstraint(
            "octet_length(recipe_bytes) BETWEEN 1 AND 4096", name=op.f("ck_companion_recipe_document_bound")
        ),
        sa.CheckConstraint("recipe_digest ~ '^[0-9a-f]{64}$'", name=op.f("ck_companion_recipe_document_sha256")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companion_recipe")),
        sa.UniqueConstraint("namespace", "key", name="uq_companion_recipe_namespace_key"),
    )


def downgrade() -> None:
    """Refuse retained aliases under an exclusive lock before dropping empty schema."""
    op.execute(sa.text("SET LOCAL statement_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    op.execute(sa.text("LOCK TABLE companion_recipe IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM companion_recipe LIMIT 1) THEN
                RAISE EXCEPTION 'Retained companion recipes prevent downgrade';
            END IF;
        END
        $$;
        """)
    )
    op.drop_table("companion_recipe")
