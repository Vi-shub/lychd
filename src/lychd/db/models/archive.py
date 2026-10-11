"""Authoritative rows for the bounded public-note Archive profile."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from advanced_alchemy.base import UUIDAuditBase
from sqlalchemy import CheckConstraint, Computed, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column


class ArchiveNamespace(UUIDAuditBase):
    __tablename__ = "archive_namespace"
    owner_subject: Mapped[str] = mapped_column(String(160))
    policy_revision: Mapped[str] = mapped_column(String(160))


class ArchiveNote(UUIDAuditBase):
    __tablename__ = "archive_note"
    __table_args__ = (
        UniqueConstraint("namespace_id", "source_key", "source_revision", name="uq_archive_note_source_revision"),
        CheckConstraint("lifecycle IN ('candidate','promoted','archived','revoked')", name="lifecycle"),
        CheckConstraint("lifecycle_version >= 1", name="lifecycle_version"),
        CheckConstraint("(lifecycle = 'revoked') = (content IS NULL)", name="revoked_content"),
        Index("ix_archive_note_search", "search_vector", postgresql_using="gin"),
        Index("ix_archive_note_namespace_lifecycle", "namespace_id", "lifecycle"),
    )
    namespace_id: Mapped[UUID] = mapped_column(ForeignKey("archive_namespace.id", ondelete="RESTRICT"))
    source_key: Mapped[str] = mapped_column(String(160))
    source_revision: Mapped[str] = mapped_column(String(160))
    source_digest: Mapped[str] = mapped_column(String(64))
    revision_digest: Mapped[str] = mapped_column(String(64))
    producer: Mapped[str] = mapped_column(String(160))
    observed_at: Mapped[datetime]
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, COALESCE(content, ''))", persisted=True)
    )
    lifecycle: Mapped[str] = mapped_column(String(16))
    lifecycle_version: Mapped[int]


class ArchiveIntake(UUIDAuditBase):
    __tablename__ = "archive_intake"
    __table_args__ = (UniqueConstraint("namespace_id", "request_id", name="uq_archive_intake_request"),)
    namespace_id: Mapped[UUID] = mapped_column(ForeignKey("archive_namespace.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    note_id: Mapped[UUID] = mapped_column(ForeignKey("archive_note.id", ondelete="RESTRICT"))


class ArchiveRecall(UUIDAuditBase):
    __tablename__ = "archive_recall"
    __table_args__ = (UniqueConstraint("namespace_id", "request_id", name="uq_archive_recall_request"),)
    namespace_id: Mapped[UUID] = mapped_column(ForeignKey("archive_namespace.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    receipt_digest: Mapped[str] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB)
