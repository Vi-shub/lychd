"""Short namespace transactions fence source disclosure and late vector publication."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer, load_only

from lychd.db.archive_derivation_lifecycle import invalidate_derivations
from lychd.db.archive_generation import exact_generation
from lychd.db.archive_namespace import lock_archive_namespace
from lychd.db.models.archive import ArchiveNote
from lychd.db.models.archive_derivation import ArchiveEmbeddingGeneration, ArchiveNoteEmbedding
from lychd.domain.animation.embeddings import embedding_vector_digest
from lychd.domain.archive.contracts import ArchiveRefused, ArchiveSubject, RecordRef, digest, text_digest
from lychd.domain.archive.derivation import (
    DerivationAttempt,
    DerivationReceipt,
    DerivationRequest,
    DerivationStatus,
    GenerationManifest,
    GenerationRef,
    GenerationStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.sql.elements import ColumnElement

    from lychd.domain.archive.ports import AuthorizationCheck


def _generation_status(row: ArchiveEmbeddingGeneration) -> GenerationStatus:
    return GenerationStatus(
        reference=GenerationRef(generation_id=row.id, manifest_digest=row.manifest_digest),
        namespace_id=row.namespace_id,
        state=cast('Literal["active", "retired"]', row.state),
        version=row.version,
        created_at=row.created_at,
        retired_at=row.retired_at,
    )


def _source(row: ArchiveNoteEmbedding) -> RecordRef:
    return RecordRef(
        record_id=row.note_id,
        revision_digest=row.source_revision_digest,
        lifecycle_version=row.source_lifecycle_version,
    )


def _status(row: ArchiveNoteEmbedding, now: datetime) -> DerivationStatus:
    receipt = DerivationReceipt.model_validate(row.receipt) if row.receipt is not None else None
    state = "expired" if row.state == "running" and row.expires_at is not None and row.expires_at <= now else row.state
    return DerivationStatus(
        derivation_id=row.id,
        namespace_id=row.namespace_id,
        generation=GenerationRef(generation_id=row.generation_id, manifest_digest=row.generation_manifest_digest),
        source=_source(row),
        source_digest=row.source_digest,
        row_version=row.row_version,
        state=cast('Literal["prepared", "running", "expired", "complete", "failed", "invalidated"]', state),
        attempt_version=row.attempt_version,
        expires_at=row.expires_at,
        observed_at=now,
        completed_at=row.completed_at,
        failure_code=row.failure_code,
        invalidated_reason=row.invalidated_reason,
        vector_digest=row.vector_digest,
        receipt_digest=receipt.receipt_digest if receipt else None,
    )


async def _now(session: AsyncSession) -> datetime:
    return cast("datetime", await session.scalar(select(func.clock_timestamp())))


async def _eligible(session: AsyncSession, manifest: GenerationManifest, reference: RecordRef) -> ArchiveNote:
    # Metadata only. Source bytes are projected separately after the claim check.
    note = await session.scalar(
        select(ArchiveNote)
        .options(
            load_only(
                ArchiveNote.id,
                ArchiveNote.namespace_id,
                ArchiveNote.revision_digest,
                ArchiveNote.source_digest,
                ArchiveNote.lifecycle,
                ArchiveNote.lifecycle_version,
            )
        )
        .where(ArchiveNote.namespace_id == manifest.archive.namespace_id, ArchiveNote.id == reference.record_id)
    )
    if (
        note is None
        or note.lifecycle != "promoted"
        or note.revision_digest != reference.revision_digest
        or note.lifecycle_version != reference.lifecycle_version
    ):
        raise ArchiveRefused("source_stale")  # noqa: EM101 - closed safe refusal code
    return note


async def _row(
    session: AsyncSession, manifest: GenerationManifest, request: DerivationRequest
) -> ArchiveNoteEmbedding | None:
    row = await session.scalar(
        select(ArchiveNoteEmbedding)
        .options(defer(ArchiveNoteEmbedding.vector))
        .where(
            ArchiveNoteEmbedding.namespace_id == manifest.archive.namespace_id,
            ArchiveNoteEmbedding.generation_id == request.generation.generation_id,
            ArchiveNoteEmbedding.note_id == request.source.record_id,
            ArchiveNoteEmbedding.chunk_ordinal == 0,
        )
    )
    if row is not None and (
        _source(row) != request.source
        or row.generation_manifest_digest != request.generation.manifest_digest
        or row.binding_digest != digest(request.model_dump(mode="json", exclude={"run_id"}))
    ):
        raise ArchiveRefused("derivation_binding")  # noqa: EM101 - closed safe refusal code
    return row


def _fence(attempt: DerivationAttempt) -> tuple[ColumnElement[bool], ...]:
    return (
        ArchiveNoteEmbedding.id == attempt.status.derivation_id,
        ArchiveNoteEmbedding.namespace_id == attempt.status.namespace_id,
        ArchiveNoteEmbedding.generation_id == attempt.status.generation.generation_id,
        ArchiveNoteEmbedding.note_id == attempt.status.source.record_id,
        ArchiveNoteEmbedding.generation_manifest_digest == attempt.status.generation.manifest_digest,
        ArchiveNoteEmbedding.source_revision_digest == attempt.status.source.revision_digest,
        ArchiveNoteEmbedding.source_lifecycle_version == attempt.status.source.lifecycle_version,
        ArchiveNoteEmbedding.source_digest == attempt.status.source_digest,
        ArchiveNoteEmbedding.binding_digest
        == digest(
            DerivationRequest(
                generation=attempt.status.generation,
                source=attempt.status.source,
            ).model_dump(mode="json", exclude={"run_id"})
        ),
        ArchiveNoteEmbedding.state == "running",
        ArchiveNoteEmbedding.row_version == attempt.status.row_version,
        ArchiveNoteEmbedding.attempt_version == attempt.status.attempt_version,
        ArchiveNoteEmbedding.attempt_token == attempt.token,
        ArchiveNoteEmbedding.subject == attempt.subject.identity,
        ArchiveNoteEmbedding.purpose == attempt.purpose,
        ArchiveNoteEmbedding.run_id.is_not_distinct_from(attempt.run_id),
        ArchiveNoteEmbedding.chunk_ordinal == 0,
        ArchiveNoteEmbedding.expires_at == attempt.status.expires_at,
        ArchiveNoteEmbedding.expires_at > func.clock_timestamp(),
    )


class DbArchiveDerivationStore:
    """No transaction survives a Core embedding call or lease cleanup."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Use the admitted runtime session factory; no alternate database owner."""
        self._session_factory = session_factory

    @asynccontextmanager
    async def _transaction(self) -> AsyncGenerator[AsyncSession]:
        """Keep SQL parameters and vector coordinates out of propagated failures."""
        try:
            async with self._session_factory() as session, session.begin():
                yield session
        except SQLAlchemyError:
            msg = "storage"
            raise ArchiveRefused(msg) from None

    async def register(
        self, manifest: GenerationManifest, reference: GenerationRef, check: AuthorizationCheck
    ) -> GenerationStatus:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive, create=True)
            check(reference.generation_id, digest(reference))
            row = await session.scalar(
                select(ArchiveEmbeddingGeneration).where(
                    ArchiveEmbeddingGeneration.id == reference.generation_id,
                    ArchiveEmbeddingGeneration.namespace_id == manifest.archive.namespace_id,
                )
            )
            if row is not None:
                row = await exact_generation(session, manifest, reference, active=False)
            else:
                same = await session.scalar(
                    select(ArchiveEmbeddingGeneration.id).where(
                        ArchiveEmbeddingGeneration.namespace_id == manifest.archive.namespace_id,
                        ArchiveEmbeddingGeneration.manifest_digest == manifest.digest,
                    )
                )
                if same is not None:
                    raise ArchiveRefused("generation_conflict")  # noqa: EM101 - closed safe refusal code
                row = ArchiveEmbeddingGeneration(
                    id=reference.generation_id,
                    namespace_id=manifest.archive.namespace_id,
                    manifest_digest=manifest.digest,
                    manifest=manifest.model_dump(mode="json"),
                    state="active",
                    version=1,
                    retired_at=None,
                )
                session.add(row)
                await session.flush()
            result = _generation_status(row)
            check(row.id, digest(result))
            return result

    async def retire(
        self, manifest: GenerationManifest, reference: GenerationRef, expected_version: int, check: AuthorizationCheck
    ) -> GenerationStatus:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(reference.generation_id, digest(reference))
            row = await exact_generation(session, manifest, reference, active=False)
            if row.version != expected_version or row.state != "active":
                raise ArchiveRefused("stale_version")  # noqa: EM101 - closed safe refusal code
            row.state, row.version, row.retired_at = "retired", row.version + 1, await _now(session)
            await invalidate_derivations(
                session, manifest.archive.namespace_id, reason="generation_retired", generation_id=row.id
            )
            await session.flush()
            result = _generation_status(row)
            check(row.id, digest(result))
            return result

    async def prepare(
        self, manifest: GenerationManifest, request: DerivationRequest, check: AuthorizationCheck
    ) -> DerivationStatus:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(request.source.record_id, digest(request))
            generation = await exact_generation(session, manifest, request.generation)
            note = await _eligible(session, manifest, request.source)
            row = await _row(session, manifest, request)
            if row is None:
                row = ArchiveNoteEmbedding(
                    id=uuid4(),
                    namespace_id=manifest.archive.namespace_id,
                    generation_id=generation.id,
                    note_id=note.id,
                    chunk_ordinal=0,
                    generation_manifest_digest=generation.manifest_digest,
                    generation_version=generation.version,
                    source_revision_digest=note.revision_digest,
                    source_digest=note.source_digest,
                    source_lifecycle_version=note.lifecycle_version,
                    binding_digest=digest(request.model_dump(mode="json", exclude={"run_id"})),
                    state="prepared",
                    row_version=1,
                    attempt_version=0,
                )
                session.add(row)
                await session.flush()
            result = _status(row, await _now(session))
            check(row.id, digest(result))
            if result.state in {"running", "invalidated"}:
                raise ArchiveRefused("derivation_busy" if result.state == "running" else "derivation_invalidated")
            return result

    async def inspect(
        self, manifest: GenerationManifest, request: DerivationRequest, check: AuthorizationCheck
    ) -> DerivationStatus:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(request.source.record_id, digest(request))
            await exact_generation(session, manifest, request.generation, active=False)
            row = await _row(session, manifest, request)
            if row is None:
                raise ArchiveRefused("derivation_missing")  # noqa: EM101 - closed safe refusal code
            result = _status(row, await _now(session))
            check(row.id, digest(result))
            return result

    async def claim(
        self,
        manifest: GenerationManifest,
        request: DerivationRequest,
        expected: DerivationStatus,
        subject: ArchiveSubject,
        purpose: str,
        check: AuthorizationCheck,
    ) -> DerivationAttempt:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(expected.derivation_id, digest(request))
            generation = await exact_generation(session, manifest, request.generation)
            note = await _eligible(session, manifest, request.source)
            row = await _row(session, manifest, request)
            now = await _now(session)
            if (
                row is None
                or row.row_version != expected.row_version
                or row.attempt_version != expected.attempt_version
                or _status(row, now).state not in {"prepared", "failed", "expired"}
                or row.generation_version != generation.version
            ):
                raise ArchiveRefused("attempt_stale")  # noqa: EM101 - closed safe refusal code
            row.state, row.row_version, row.attempt_version = "running", row.row_version + 1, row.attempt_version + 1
            token = uuid4()
            row.attempt_token, row.started_at, row.expires_at = (
                token,
                now,
                now + timedelta(seconds=manifest.attempt_seconds),
            )
            row.subject, row.purpose, row.run_id = subject.identity, purpose, request.run_id
            row.vector = row.vector_digest = row.receipt = row.completed_at = row.failure_code = None
            await session.flush()
            status = _status(row, now)
            check(row.id, digest(status))
            content = await session.scalar(
                select(ArchiveNote.content).where(
                    ArchiveNote.id == note.id,
                    ArchiveNote.namespace_id == manifest.archive.namespace_id,
                    ArchiveNote.lifecycle == "promoted",
                    ArchiveNote.lifecycle_version == request.source.lifecycle_version,
                    ArchiveNote.revision_digest == request.source.revision_digest,
                    ArchiveNote.source_digest == row.source_digest,
                )
            )
            if content is None or text_digest(content) != row.source_digest:
                raise ArchiveRefused("source_integrity")  # noqa: EM101 - closed safe refusal code
            attempt = DerivationAttempt(
                status=status,
                token=token,
                subject=subject,
                purpose=purpose,
                run_id=request.run_id,
                text=content,
            )
            check(row.id, attempt.binding_digest)
            return attempt

    async def complete(
        self,
        manifest: GenerationManifest,
        attempt: DerivationAttempt,
        receipt: DerivationReceipt,
        vector: tuple[float, ...],
        check: AuthorizationCheck,
    ) -> DerivationStatus:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(attempt.status.derivation_id, attempt.binding_digest)
            generation = await exact_generation(session, manifest, attempt.status.generation)
            note = await _eligible(session, manifest, attempt.status.source)
            if note.source_digest != attempt.status.source_digest:
                raise ArchiveRefused("source_integrity")  # noqa: EM101 - closed safe refusal code
            statement = (
                update(ArchiveNoteEmbedding)
                .where(
                    *_fence(attempt),
                    ArchiveNoteEmbedding.generation_version == generation.version,
                    ArchiveNoteEmbedding.generation_manifest_digest == generation.manifest_digest,
                    ArchiveNoteEmbedding.source_revision_digest == note.revision_digest,
                    ArchiveNoteEmbedding.source_lifecycle_version == note.lifecycle_version,
                    ArchiveNoteEmbedding.source_digest == note.source_digest,
                )
                .values(
                    state="complete",
                    row_version=ArchiveNoteEmbedding.row_version + 1,
                    attempt_token=None,
                    expires_at=None,
                    vector=list(vector),
                    vector_digest=receipt.vector_digest,
                    receipt=receipt.model_dump(mode="json"),
                    completed_at=func.clock_timestamp(),
                    failure_code=None,
                )
                .returning(ArchiveNoteEmbedding.id)
            )
            changed = await session.scalar(statement)
            if changed is None:
                raise ArchiveRefused("attempt_stale")  # noqa: EM101 - closed safe refusal code
            row = await session.scalar(
                select(ArchiveNoteEmbedding)
                .where(ArchiveNoteEmbedding.id == changed)
                .execution_options(populate_existing=True)
            )
            if row is None or row.vector is None:
                raise ArchiveRefused("attempt_stale")  # noqa: EM101 - closed safe refusal code
            # pgvector's actual returned float32 bytes must retain the Core digest, including -0.
            if embedding_vector_digest(tuple(float(value) for value in row.vector)) != receipt.vector_digest:
                raise ArchiveRefused("vector_storage_integrity")  # noqa: EM101 - closed safe refusal code
            result = _status(row, await _now(session))
            check(row.id, digest(result))
            return result

    async def fail(self, manifest: GenerationManifest, attempt: DerivationAttempt, check: AuthorizationCheck) -> None:
        async with self._transaction() as session:
            await lock_archive_namespace(session, manifest.archive)
            check(attempt.status.derivation_id, attempt.binding_digest)
            await session.execute(
                update(ArchiveNoteEmbedding)
                .where(*_fence(attempt))
                .values(
                    state="failed",
                    row_version=ArchiveNoteEmbedding.row_version + 1,
                    attempt_token=None,
                    expires_at=None,
                    failure_code="execution_failed",
                )
            )
            check(attempt.status.derivation_id, attempt.binding_digest)
