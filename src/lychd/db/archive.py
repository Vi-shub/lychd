"""PostgreSQL Archive operations serialized by one retained namespace binding."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func, select

from lychd.db.archive_derivation_lifecycle import invalidate_derivations
from lychd.db.archive_namespace import lock_archive_namespace
from lychd.db.models.archive import ArchiveIntake, ArchiveNote, ArchiveRecall
from lychd.domain.archive.contracts import (
    ArchiveProfile,
    ArchiveRefused,
    ArchiveSubject,
    Lifecycle,
    NoteProposal,
    NoteRecord,
    RecallItem,
    RecallProjection,
    RecallReceipt,
    RecallRequest,
    RecallSelection,
    RecordRef,
    digest,
    text_digest,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from lychd.domain.archive.ports import AuthorizationCheck


def _record(note: ArchiveNote) -> NoteRecord:
    return NoteRecord(
        namespace_id=note.namespace_id,
        reference=RecordRef(
            record_id=note.id, revision_digest=note.revision_digest, lifecycle_version=note.lifecycle_version
        ),
        source_key=note.source_key,
        source_revision=note.source_revision,
        source_digest=note.source_digest,
        producer=note.producer,
        observed_at=note.observed_at,
        lifecycle=Lifecycle(note.lifecycle),
        created_at=note.created_at,
    )


async def _note(session: AsyncSession, namespace_id: UUID, record_id: UUID) -> ArchiveNote:
    note = await session.scalar(
        select(ArchiveNote).where(ArchiveNote.namespace_id == namespace_id, ArchiveNote.id == record_id)
    )
    if note is None:
        msg = "record_missing"
        raise ArchiveRefused(msg)
    return note


def _receipt(row: ArchiveRecall) -> RecallReceipt:
    receipt = RecallReceipt.model_validate(row.receipt)
    if receipt.receipt_digest != row.receipt_digest or receipt.computed_digest() != row.receipt_digest:
        msg = "receipt_integrity"
        raise ArchiveRefused(msg)
    return receipt


class DbArchiveStore:
    """One database, current checks and exact revisions; no in-memory authority cache."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """Bind the already admitted application session factory."""
        self._session_factory = session_factory

    async def admit(self, profile: ArchiveProfile, proposal: NoteProposal, check: AuthorizationCheck) -> NoteRecord:
        request_digest = digest(proposal)
        revision_digest = digest(proposal.source_binding())
        async with self._session_factory() as session, session.begin():
            await lock_archive_namespace(session, profile, create=True)
            check(proposal.request_id, request_digest)
            intake = await session.scalar(
                select(ArchiveIntake).where(
                    ArchiveIntake.namespace_id == profile.namespace_id, ArchiveIntake.request_id == proposal.request_id
                )
            )
            if intake is not None:
                if intake.request_digest != request_digest:
                    msg = "request_conflict"
                    raise ArchiveRefused(msg)
                note = await _note(session, profile.namespace_id, intake.note_id)
                if note.lifecycle == Lifecycle.REVOKED:
                    msg = "revoked"
                    raise ArchiveRefused(msg)
                check(note.id, request_digest)
                return _record(note)
            note = await session.scalar(
                select(ArchiveNote).where(
                    ArchiveNote.namespace_id == profile.namespace_id,
                    ArchiveNote.source_key == proposal.source_key,
                    ArchiveNote.source_revision == proposal.source_revision,
                )
            )
            if note is not None:
                if note.revision_digest != revision_digest:
                    msg = "source_conflict"
                    raise ArchiveRefused(msg)
                if note.lifecycle == Lifecycle.REVOKED:
                    msg = "revoked"
                    raise ArchiveRefused(msg)
            else:
                note = ArchiveNote(
                    id=uuid4(),
                    namespace_id=profile.namespace_id,
                    source_key=proposal.source_key,
                    source_revision=proposal.source_revision,
                    source_digest=proposal.source_digest,
                    revision_digest=revision_digest,
                    producer=proposal.producer,
                    observed_at=proposal.observed_at,
                    content=proposal.text,
                    lifecycle=Lifecycle.CANDIDATE,
                    lifecycle_version=1,
                )
                check(note.id, request_digest)
                session.add(note)
                await session.flush()
            check(note.id, request_digest)
            session.add(
                ArchiveIntake(
                    namespace_id=profile.namespace_id,
                    request_id=proposal.request_id,
                    request_digest=request_digest,
                    note_id=note.id,
                )
            )
            await session.flush()
            check(note.id, request_digest)
            return _record(note)

    async def transition(
        self,
        profile: ArchiveProfile,
        record_id: UUID,
        expected_version: int,
        target: Lifecycle,
        check: AuthorizationCheck,
    ) -> NoteRecord:
        async with self._session_factory() as session, session.begin():
            await lock_archive_namespace(session, profile)
            check(
                record_id, digest({"record_id": str(record_id), "expected_version": expected_version, "target": target})
            )
            note = await _note(session, profile.namespace_id, record_id)
            if note.lifecycle_version != expected_version:
                msg = "stale_version"
                raise ArchiveRefused(msg)
            if note.lifecycle == Lifecycle.REVOKED:
                msg = "revoked"
                raise ArchiveRefused(msg)
            allowed = {
                Lifecycle.PROMOTED: {Lifecycle.CANDIDATE},
                Lifecycle.ARCHIVED: {Lifecycle.PROMOTED},
                Lifecycle.REVOKED: {Lifecycle.CANDIDATE, Lifecycle.PROMOTED, Lifecycle.ARCHIVED},
            }
            if target not in allowed or Lifecycle(note.lifecycle) not in allowed[target]:
                msg = "transition"
                raise ArchiveRefused(msg)
            predecessors: list[ArchiveNote] = []
            if target is Lifecycle.PROMOTED:
                predecessors = list(
                    (
                        await session.scalars(
                            select(ArchiveNote).where(
                                ArchiveNote.namespace_id == profile.namespace_id,
                                ArchiveNote.source_key == note.source_key,
                                ArchiveNote.lifecycle == Lifecycle.PROMOTED,
                                ArchiveNote.id != note.id,
                            )
                        )
                    ).all()
                )
            binding = digest(
                {
                    "record": _record(note).model_dump(mode="json"),
                    "target": target,
                    "superseded": [_record(item).model_dump(mode="json") for item in predecessors],
                }
            )
            check(note.id, binding)
            for prior in predecessors:
                prior.lifecycle = Lifecycle.ARCHIVED
                prior.lifecycle_version += 1
            note.lifecycle = target
            note.lifecycle_version += 1
            if target is Lifecycle.REVOKED:
                note.content = None
            invalidated = tuple(prior.id for prior in predecessors)
            if target in {Lifecycle.ARCHIVED, Lifecycle.REVOKED}:
                invalidated += (note.id,)
            if invalidated:
                await invalidate_derivations(session, profile.namespace_id, reason=target.value, note_ids=invalidated)
            await session.flush()
            check(note.id, binding)
            return _record(note)

    async def recall(
        self,
        profile: ArchiveProfile,
        subject: ArchiveSubject,
        request: RecallRequest,
        purpose: str,
        check: AuthorizationCheck,
    ) -> RecallReceipt:
        request_digest = digest(
            {
                "request": request.model_dump(mode="json"),
                "subject": subject.model_dump(),
                "purpose": purpose,
                "profile": profile.model_dump(mode="json"),
            }
        )
        async with self._session_factory() as session, session.begin():
            await lock_archive_namespace(session, profile, create=True)
            check(request.request_id, request_digest)
            existing = await session.scalar(
                select(ArchiveRecall).where(
                    ArchiveRecall.namespace_id == profile.namespace_id,
                    ArchiveRecall.request_id == request.request_id,
                )
            )
            if existing is not None:
                if existing.request_digest != request_digest:
                    msg = "request_conflict"
                    raise ArchiveRefused(msg)
                receipt = _receipt(existing)
                check(existing.id, receipt.receipt_digest)
                return receipt
            query = func.plainto_tsquery("simple", request.query)
            score = func.ts_rank_cd(ArchiveNote.search_vector, query, 32)
            matches = (
                await session.execute(
                    select(ArchiveNote, score.label("score"))
                    .where(
                        ArchiveNote.namespace_id == profile.namespace_id,
                        ArchiveNote.lifecycle == Lifecycle.PROMOTED,
                        ArchiveNote.content.is_not(None),
                        ArchiveNote.search_vector.op("@@")(query),
                        score >= request.min_score,
                    )
                    .order_by(score.desc(), ArchiveNote.id)
                    .limit(request.max_results)
                )
            ).all()
            selected: list[RecallSelection] = []
            remaining = request.char_budget
            omitted = 0
            for note, rank in matches:
                if note.content is None:
                    msg = "record_integrity"
                    raise ArchiveRefused(msg)
                chars = len(note.content)
                if chars > remaining:
                    omitted += 1
                    continue
                remaining -= chars
                selected.append(
                    RecallSelection(
                        reference=_record(note).reference,
                        source_key=note.source_key,
                        source_revision=note.source_revision,
                        source_digest=note.source_digest,
                        score=float(rank),
                        projection_digest=text_digest(note.content),
                        projected_chars=chars,
                    )
                )
            receipt = RecallReceipt(
                receipt_id=uuid4(),
                request_id=request.request_id,
                namespace_id=profile.namespace_id,
                subject=subject,
                purpose=purpose,
                policy_revision=profile.policy_revision,
                query_digest=text_digest(request.query),
                request_digest=request_digest,
                run_id=request.run_id,
                max_results=request.max_results,
                char_budget=request.char_budget,
                min_score=request.min_score,
                selected=tuple(selected),
                omitted_for_budget=omitted,
                created_at=datetime.now(UTC),
                receipt_digest="0" * 64,
            )
            receipt = receipt.model_copy(update={"receipt_digest": receipt.computed_digest()})
            check(receipt.receipt_id, receipt.receipt_digest)
            session.add(
                ArchiveRecall(
                    id=receipt.receipt_id,
                    namespace_id=profile.namespace_id,
                    request_id=request.request_id,
                    request_digest=request_digest,
                    receipt_digest=receipt.receipt_digest,
                    receipt=receipt.model_dump(mode="json"),
                )
            )
            await session.flush()
            check(receipt.receipt_id, receipt.receipt_digest)
            return receipt

    async def resolve(
        self,
        profile: ArchiveProfile,
        subject: ArchiveSubject,
        receipt_id: UUID,
        receipt_digest: str,
        purpose: str,
        check: AuthorizationCheck,
    ) -> RecallProjection:
        async with self._session_factory() as session, session.begin():
            await lock_archive_namespace(session, profile)
            check(receipt_id, receipt_digest)
            row = await session.scalar(
                select(ArchiveRecall).where(
                    ArchiveRecall.namespace_id == profile.namespace_id,
                    ArchiveRecall.id == receipt_id,
                )
            )
            if row is None:
                msg = "receipt_missing"
                raise ArchiveRefused(msg)
            receipt = _receipt(row)
            if (receipt.receipt_digest, receipt.subject, receipt.purpose, receipt.policy_revision) != (
                receipt_digest,
                subject,
                purpose,
                profile.policy_revision,
            ):
                msg = "receipt_binding"
                raise ArchiveRefused(msg)
            items: list[RecallItem] = []
            for selection in receipt.selected:
                note = await _note(session, profile.namespace_id, selection.reference.record_id)
                if (
                    note.lifecycle != Lifecycle.PROMOTED
                    or note.content is None
                    or _record(note).reference != selection.reference
                    or text_digest(note.content) != selection.projection_digest
                    or len(note.content) != selection.projected_chars
                ):
                    msg = "receipt_stale"
                    raise ArchiveRefused(msg)
                items.append(RecallItem(selection=selection, text=note.content))
            projection = RecallProjection(receipt=receipt, items=tuple(items))
            check(receipt_id, receipt_digest)
            return projection
