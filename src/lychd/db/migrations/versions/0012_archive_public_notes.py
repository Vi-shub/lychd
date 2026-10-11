"""Retain explicitly admitted public notes and text-free lexical recall receipts.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _audit() -> list[sa.Column[UUID] | sa.Column[int] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _namespace() -> sa.Column[UUID]:
    return sa.Column(
        "namespace_id", sa.UUID(), sa.ForeignKey("archive_namespace.id", ondelete="RESTRICT"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "archive_namespace",
        *_audit(),
        sa.Column("owner_subject", sa.String(160), nullable=False),
        sa.Column("policy_revision", sa.String(160), nullable=False),
    )
    op.create_table(
        "archive_note",
        *_audit(),
        _namespace(),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.String(160), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("revision_digest", sa.String(64), nullable=False),
        sa.Column("producer", sa.String(160), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, COALESCE(content, ''))", persisted=True),
            nullable=False,
        ),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("lifecycle_version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("namespace_id", "source_key", "source_revision", name="uq_archive_note_source_revision"),
        sa.CheckConstraint(
            "lifecycle IN ('candidate','promoted','archived','revoked')", name=op.f("ck_archive_note_lifecycle")
        ),
        sa.CheckConstraint("lifecycle_version >= 1", name=op.f("ck_archive_note_lifecycle_version")),
        sa.CheckConstraint("(lifecycle = 'revoked') = (content IS NULL)", name=op.f("ck_archive_note_revoked_content")),
    )
    op.create_index("ix_archive_note_search", "archive_note", ["search_vector"], postgresql_using="gin")
    op.create_index("ix_archive_note_namespace_lifecycle", "archive_note", ["namespace_id", "lifecycle"])
    op.create_table(
        "archive_intake",
        *_audit(),
        _namespace(),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("note_id", sa.UUID(), sa.ForeignKey("archive_note.id", ondelete="RESTRICT"), nullable=False),
        sa.UniqueConstraint("namespace_id", "request_id", name="uq_archive_intake_request"),
    )
    op.create_table(
        "archive_recall",
        *_audit(),
        _namespace(),
        sa.Column("request_id", sa.UUID(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("receipt_digest", sa.String(64), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("namespace_id", "request_id", name="uq_archive_recall_request"),
    )


def downgrade() -> None:
    """Refuse history loss while excluding concurrent writers from the emptiness check."""
    for table in ("archive_namespace", "archive_note", "archive_intake", "archive_recall"):
        op.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM archive_namespace LIMIT 1)
         OR EXISTS (SELECT 1 FROM archive_note LIMIT 1)
         OR EXISTS (SELECT 1 FROM archive_intake LIMIT 1)
         OR EXISTS (SELECT 1 FROM archive_recall LIMIT 1) THEN
        RAISE EXCEPTION 'Archive history requires explicit retirement before downgrade';
      END IF;
    END $$;""")
    )
    op.drop_table("archive_recall")
    op.drop_table("archive_intake")
    op.drop_table("archive_note")
    op.drop_table("archive_namespace")
