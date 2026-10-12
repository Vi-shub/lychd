"""Retain public-note generation and attempt fences without semantic retrieval.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _audit() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_unique_constraint("uq_archive_note_namespace_id", "archive_note", ["namespace_id", "id"])
    op.create_table(
        "archive_embedding_generation",
        *_audit(),
        sa.Column(
            "namespace_id", sa.UUID(), sa.ForeignKey("archive_namespace.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("namespace_id", "id", name="uq_archive_generation_namespace_id"),
        sa.UniqueConstraint("namespace_id", "manifest_digest", name="uq_archive_generation_manifest"),
        sa.CheckConstraint("state IN ('active','retired')", name=op.f("ck_archive_embedding_generation_state")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_archive_embedding_generation_version")),
        sa.CheckConstraint(
            "(state = 'retired') = (retired_at IS NOT NULL)", name=op.f("ck_archive_embedding_generation_retirement")
        ),
    )
    op.create_table(
        "archive_note_embedding",
        *_audit(),
        sa.Column("namespace_id", sa.UUID(), nullable=False),
        sa.Column("generation_id", sa.UUID(), nullable=False),
        sa.Column("note_id", sa.UUID(), nullable=False),
        sa.Column("chunk_ordinal", sa.Integer(), nullable=False),
        sa.Column("generation_manifest_digest", sa.String(64), nullable=False),
        sa.Column("generation_version", sa.Integer(), nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("source_lifecycle_version", sa.Integer(), nullable=False),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("attempt_version", sa.Integer(), nullable=False),
        sa.Column("attempt_token", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subject", sa.String(160), nullable=True),
        sa.Column("purpose", sa.String(160), nullable=True),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("vector", Vector(1024), nullable=True),
        sa.Column("vector_digest", sa.String(64), nullable=True),
        sa.Column("receipt", JSONB(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("invalidated_reason", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(
            ["namespace_id", "generation_id"],
            ["archive_embedding_generation.namespace_id", "archive_embedding_generation.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["namespace_id", "note_id"], ["archive_note.namespace_id", "archive_note.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "namespace_id", "generation_id", "note_id", "chunk_ordinal", name="uq_archive_embedding_source"
        ),
        sa.CheckConstraint("chunk_ordinal = 0", name=op.f("ck_archive_note_embedding_whole_note")),
        sa.CheckConstraint(
            "state IN ('prepared','running','complete','failed','invalidated')",
            name=op.f("ck_archive_note_embedding_state"),
        ),
        sa.CheckConstraint(
            "row_version >= 1 AND attempt_version >= 0 AND source_lifecycle_version >= 1 AND generation_version >= 1",
            name=op.f("ck_archive_note_embedding_versions"),
        ),
        sa.CheckConstraint(
            "(state = 'running') = (attempt_token IS NOT NULL AND expires_at IS NOT NULL)",
            name=op.f("ck_archive_note_embedding_running_fence"),
        ),
        sa.CheckConstraint(
            "(attempt_token IS NULL) = (expires_at IS NULL)", name=op.f("ck_archive_note_embedding_fence_pair")
        ),
        sa.CheckConstraint(
            "state <> 'running' OR (started_at IS NOT NULL AND subject IS NOT NULL AND purpose IS NOT NULL)",
            name=op.f("ck_archive_note_embedding_attempt_binding"),
        ),
        sa.CheckConstraint(
            "(state = 'complete') = (vector IS NOT NULL)", name=op.f("ck_archive_note_embedding_complete_vector")
        ),
        sa.CheckConstraint(
            "state <> 'complete' OR (vector_digest IS NOT NULL AND receipt IS NOT NULL AND completed_at IS NOT NULL)",
            name=op.f("ck_archive_note_embedding_complete_receipt"),
        ),
    )
    op.create_index(
        "ix_archive_embedding_generation_state", "archive_note_embedding", ["namespace_id", "generation_id", "state"]
    )
    op.create_index("ix_archive_embedding_note", "archive_note_embedding", ["namespace_id", "note_id"])


def downgrade() -> None:
    """Keep tombstones and refuse history loss while excluding concurrent writers."""
    for table in ("archive_namespace", "archive_note", "archive_embedding_generation", "archive_note_embedding"):
        op.execute(sa.text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
    op.execute(
        sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM archive_embedding_generation LIMIT 1)
         OR EXISTS (SELECT 1 FROM archive_note_embedding LIMIT 1) THEN
        RAISE EXCEPTION 'Retained Archive derivation history prevents downgrade';
      END IF;
    END $$;""")
    )
    op.drop_table("archive_note_embedding")
    op.drop_table("archive_embedding_generation")
    op.drop_constraint("uq_archive_note_namespace_id", "archive_note", type_="unique")
