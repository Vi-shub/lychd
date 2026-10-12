"""Bounded durable identities for explicitly selected public-note derivations."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from lychd.domain.animation.embeddings import EmbeddingBinding, EmbeddingProfile
from lychd.domain.archive.contracts import ArchiveProfile, ArchiveSubject, ArchiveValue, Digest, RecordRef, digest

VECTOR_DIMENSION = 1024
MAX_EMBED_SECONDS = 40


class GenerationManifest(ArchiveValue):
    """Immutable source, representation and expiry law; never supplied by HTTP callers."""

    archive: ArchiveProfile
    binding: EmbeddingBinding
    embedding: EmbeddingProfile
    chunker: Literal["whole-note@1"] = "whole-note@1"
    storage: Literal["pgvector-f32@1"] = "pgvector-f32@1"
    metric: Literal["cosine"] = "cosine"
    classification: Literal["public"] = "public"
    attempt_seconds: Literal[60] = 60

    @model_validator(mode="after")
    def compatible(self) -> GenerationManifest:
        if (
            self.binding.profile_digest != self.embedding.digest
            or self.embedding.dimension != VECTOR_DIMENSION
            or self.embedding.max_batch_items != 1
            or self.embedding.timeout_seconds > MAX_EMBED_SECONDS
        ):
            msg = "Derivation requires the exact bounded 1024-dimensional embedding profile."
            raise ValueError(msg)
        return self

    @property
    def digest(self) -> str:
        return digest(self)


class GenerationRef(ArchiveValue):
    generation_id: UUID
    manifest_digest: Digest


class GenerationStatus(ArchiveValue):
    reference: GenerationRef
    namespace_id: UUID
    state: Literal["active", "retired"]
    version: Annotated[int, Field(ge=1)]
    created_at: datetime
    retired_at: datetime | None


class DerivationRequest(ArchiveValue):
    generation: GenerationRef
    source: RecordRef
    run_id: UUID | None = None


class DerivationStatus(ArchiveValue):
    """Latest attempt only: no source, vector, provider response or attempt token."""

    derivation_id: UUID
    namespace_id: UUID
    generation: GenerationRef
    source: RecordRef
    source_digest: Digest
    row_version: Annotated[int, Field(ge=1)]
    state: Literal["prepared", "running", "expired", "complete", "failed", "invalidated"]
    attempt_version: Annotated[int, Field(ge=0)]
    expires_at: datetime | None
    observed_at: datetime
    completed_at: datetime | None
    failure_code: str | None
    invalidated_reason: str | None
    vector_digest: Digest | None
    receipt_digest: Digest | None


class DerivationAttempt(ArchiveValue):
    """Internal fence; transient source cannot enter status or a durable receipt."""

    status: DerivationStatus
    token: UUID
    subject: ArchiveSubject
    purpose: str
    run_id: UUID | None
    text: str = Field(repr=False, exclude=True)

    @property
    def binding_digest(self) -> str:
        return digest(self)


class DerivationReceipt(ArchiveValue):
    """Whitelist of Core execution metadata; vector coordinates have one storage owner."""

    profile_digest: Digest
    operation: Literal["embed_document"] = "embed_document"
    item_id: str
    input_digest: Digest
    rendered_digest: Digest
    vector_digest: Digest
    tokens: Annotated[int, Field(ge=1, le=511)]
    receipt_digest: Digest
