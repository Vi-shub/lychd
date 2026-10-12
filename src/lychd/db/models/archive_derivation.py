"""Generation tombstones and latest-attempt CAS fences for public-note vectors."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ArchiveEmbeddingGeneration(UUIDAuditBase):
    __tablename__ = "archive_embedding_generation"
    __table_args__ = (
        UniqueConstraint("namespace_id", "id", name="uq_archive_generation_namespace_id"),
        UniqueConstraint("namespace_id", "manifest_digest", name="uq_archive_generation_manifest"),
        CheckConstraint("state IN ('active','retired')", name="state"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("(state = 'retired') = (retired_at IS NOT NULL)", name="retirement"),
    )
    namespace_id: Mapped[UUID] = mapped_column(ForeignKey("archive_namespace.id", ondelete="RESTRICT"))
    manifest_digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    version: Mapped[int]
    retired_at: Mapped[datetime | None]


class ArchiveNoteEmbedding(UUIDAuditBase):
    __tablename__ = "archive_note_embedding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["namespace_id", "generation_id"],
            ["archive_embedding_generation.namespace_id", "archive_embedding_generation.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["namespace_id", "note_id"], ["archive_note.namespace_id", "archive_note.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint(
            "namespace_id", "generation_id", "note_id", "chunk_ordinal", name="uq_archive_embedding_source"
        ),
        CheckConstraint("chunk_ordinal = 0", name="whole_note"),
        CheckConstraint("state IN ('prepared','running','complete','failed','invalidated')", name="state"),
        CheckConstraint(
            "row_version >= 1 AND attempt_version >= 0 AND source_lifecycle_version >= 1 AND generation_version >= 1",
            name="versions",
        ),
        CheckConstraint(
            "(state = 'running') = (attempt_token IS NOT NULL AND expires_at IS NOT NULL)", name="running_fence"
        ),
        CheckConstraint("(attempt_token IS NULL) = (expires_at IS NULL)", name="fence_pair"),
        CheckConstraint(
            "state <> 'running' OR (started_at IS NOT NULL AND subject IS NOT NULL AND purpose IS NOT NULL)",
            name="attempt_binding",
        ),
        CheckConstraint("(state = 'complete') = (vector IS NOT NULL)", name="complete_vector"),
        CheckConstraint(
            "state <> 'complete' OR (vector_digest IS NOT NULL AND receipt IS NOT NULL AND completed_at IS NOT NULL)",
            name="complete_receipt",
        ),
        Index("ix_archive_embedding_generation_state", "namespace_id", "generation_id", "state"),
        Index("ix_archive_embedding_note", "namespace_id", "note_id"),
    )
    namespace_id: Mapped[UUID]
    generation_id: Mapped[UUID]
    note_id: Mapped[UUID]
    chunk_ordinal: Mapped[int]
    generation_manifest_digest: Mapped[str] = mapped_column(String(64))
    generation_version: Mapped[int]
    source_revision_digest: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(64))
    source_lifecycle_version: Mapped[int]
    binding_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    row_version: Mapped[int]
    attempt_version: Mapped[int]
    attempt_token: Mapped[UUID | None]
    expires_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    subject: Mapped[str | None] = mapped_column(String(160))
    purpose: Mapped[str | None] = mapped_column(String(160))
    run_id: Mapped[UUID | None]
    vector: Mapped[Any | None] = mapped_column(Vector(1024), nullable=True)
    vector_digest: Mapped[str | None] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    completed_at: Mapped[datetime | None]
    failure_code: Mapped[str | None] = mapped_column(String(64))
    invalidated_reason: Mapped[str | None] = mapped_column(String(32))
