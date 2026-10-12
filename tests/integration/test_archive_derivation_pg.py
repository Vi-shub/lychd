"""Real PostgreSQL derivation fences with a deterministic Core lease seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lychd.db.archive_derivation import DbArchiveDerivationStore
from lychd.db.models.archive_derivation import ArchiveEmbeddingGeneration, ArchiveNoteEmbedding
from lychd.domain.animation.embeddings import EmbeddingBatch, EmbeddingBinding, embedding_vector_digest
from lychd.domain.archive import ArchiveRefused, ArchiveService
from lychd.domain.archive.derivation import DerivationRequest, GenerationManifest
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE as EMBEDDING_PROFILE
from tests.integration import test_archive_pg as notes
from tests.support.archive_derivation import Embeddings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.container]
pg_url = notes.pg_url
factory = notes.factory


def service(
    factory: async_sessionmaker[AsyncSession],
    authority: notes.Authority,
    driver: Embeddings,
    generation_id: UUID | None = None,
) -> ArchiveDerivationService:
    return ArchiveDerivationService(
        generation_id=generation_id or uuid4(),
        manifest=GenerationManifest(
            archive=authority.profile,
            binding=EmbeddingBinding(capability_key="local:embedding:fixed", profile_digest=EMBEDDING_PROFILE.digest),
            embedding=EMBEDDING_PROFILE,
        ),
        store=DbArchiveDerivationStore(factory),
        authorizer=authority,
        dispatcher=driver,
    )


async def setup(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[notes.Authority, Embeddings, ArchiveService, ArchiveDerivationService, DerivationRequest]:
    authority, driver = notes.Authority(), Embeddings()
    archive = notes.service(factory, authority)
    note = await notes.promote(archive)
    derivation = service(factory, authority, driver)
    await derivation.register(notes.SUBJECT, derivation.reference, purpose="derive")
    request = DerivationRequest(generation=derivation.reference, source=note.reference)
    return authority, driver, archive, derivation, request


async def test_persistent_roundtrip_replay_signed_zero_and_revoked_status(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, driver, archive, derivation, request = await setup(factory)
    result = await derivation.derive(notes.SUBJECT, request, purpose="derive")
    assert result.state == "complete"
    assert driver.calls == driver.closed == 1
    restarted = service(factory, authority, driver, derivation.reference.generation_id)
    assert (await restarted.inspect(notes.SUBJECT, request, purpose="status")).receipt_digest == result.receipt_digest
    assert (await restarted.derive(notes.SUBJECT, request, purpose="derive")).vector_digest == result.vector_digest
    assert driver.calls == 1
    async with factory() as session:
        row = await session.get(ArchiveNoteEmbedding, result.derivation_id)
        assert row is not None
        assert row.vector is not None
        assert embedding_vector_digest(tuple(float(v) for v in row.vector)) == result.vector_digest
        assert row.receipt is not None
        assert "vector" not in row.receipt
        assert row.receipt["vector_digest"] == result.vector_digest
        assert float(row.vector[1]).hex() == "-0x0.0p+0"
    await archive.revoke(notes.SUBJECT, request.source.record_id, expected_version=2, purpose="manage")
    status = await restarted.inspect(notes.SUBJECT, request, purpose="status")
    assert status.state == "invalidated"
    assert status.source == request.source
    async with factory() as session:
        row = await session.get(ArchiveNoteEmbedding, result.derivation_id)
        assert row is not None
        assert row.vector is None
        assert row.attempt_token is None
    with pytest.raises(ArchiveRefused, match="source_stale"):
        await restarted.derive(notes.SUBJECT, request, purpose="derive")


@pytest.mark.parametrize("mutation", ["archive", "revoke", "promote", "retire"])
@pytest.mark.parametrize("completion_first", [False, True])
async def test_lifecycle_serializes_with_completion(
    factory: async_sessionmaker[AsyncSession], mutation: str, *, completion_first: bool
) -> None:
    _, driver, archive, derivation, request = await setup(factory)
    if not completion_first:
        driver.release.clear()
    work = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.entered.wait()
    if completion_first:
        assert (await work).state == "complete"
    if mutation == "retire":
        await derivation.retire(notes.SUBJECT, derivation.reference, expected_version=1, purpose="manage")
    elif mutation == "promote":
        await notes.promote(archive, notes.proposal(source_revision="2", text="Corrected public fox."))
    else:
        await getattr(archive, mutation)(notes.SUBJECT, request.source.record_id, expected_version=2, purpose="manage")
    if not completion_first:
        driver.release.set()
        with pytest.raises(ArchiveRefused, match=r"source_stale|generation_retired"):
            await work
    result = await derivation.inspect(notes.SUBJECT, request, purpose="status")
    assert result.state == "invalidated"
    async with factory() as session:
        row = await session.get(ArchiveNoteEmbedding, result.derivation_id)
        assert row is not None
        assert row.vector is None
        assert row.attempt_token is None
    assert driver.closed == 1


async def test_explicit_expired_takeover_fences_old_result(factory: async_sessionmaker[AsyncSession]) -> None:
    authority, old_driver, _, old, request = await setup(factory)
    old_driver.release.clear()
    work = asyncio.create_task(old.derive(notes.SUBJECT, request, purpose="derive"))
    await old_driver.entered.wait()
    with pytest.raises(ArchiveRefused, match="derivation_busy"):
        await old.derive(notes.SUBJECT, request, purpose="derive")
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE archive_note_embedding SET expires_at=clock_timestamp()-interval '1 second'")
        )
    assert (await old.inspect(notes.SUBJECT, request, purpose="status")).state == "expired"
    driver = Embeddings()
    restarted = service(factory, authority, driver, request.generation.generation_id)
    winner = await restarted.derive(notes.SUBJECT, request, purpose="derive")
    assert winner.state == "complete"
    assert winner.attempt_version == 2
    old_driver.release.set()
    with pytest.raises(ArchiveRefused, match="attempt_stale"):
        await work
    assert (await old.inspect(notes.SUBJECT, request, purpose="status")).row_version == winner.row_version


async def test_duplicate_claims_only_one_source_handoff(factory: async_sessionmaker[AsyncSession]) -> None:
    _, driver, _, derivation, request = await setup(factory)
    driver.release.clear()
    first = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.entered.wait()
    with pytest.raises(ArchiveRefused, match="derivation_busy"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    driver.release.set()
    assert (await first).state == "complete"
    assert driver.calls == 1


@pytest.mark.parametrize("at", ["claim_exit", "completion_exit"])
async def test_current_authority_after_actual_session_cleanup(
    factory: async_sessionmaker[AsyncSession], at: str
) -> None:
    authority, driver, _, existing, request = await setup(factory)
    armed = False

    class Session(AsyncSession):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            if armed:
                authority.allowed = False

    bound = async_sessionmaker(factory.kw["bind"], class_=Session, expire_on_commit=False)

    def arm() -> None:
        nonlocal armed
        armed = True

    if at == "claim_exit":
        driver.on_enter = arm
    else:
        driver.on_close = arm
    derivation = service(
        cast("async_sessionmaker[AsyncSession]", bound), authority, driver, existing.reference.generation_id
    )
    with pytest.raises(ArchiveRefused, match="authority"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    authority.allowed = True
    retained = await existing.inspect(notes.SUBJECT, request, purpose="status")
    assert driver.calls == (0 if at == "claim_exit" else 1)
    assert retained.state == ("running" if at == "claim_exit" else "complete")


async def test_cleanup_failure_never_publishes_and_can_retry(factory: async_sessionmaker[AsyncSession]) -> None:
    _, driver, _, derivation, request = await setup(factory)
    driver.fail_close = True
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    assert (await derivation.inspect(notes.SUBJECT, request, purpose="status")).state == "failed"
    driver.fail_close = False
    assert (await derivation.derive(notes.SUBJECT, request, purpose="derive")).attempt_version == 2


@pytest.mark.parametrize("wrong", ["profile", "input", "receipt", "vector"])
async def test_malformed_output_cannot_publish(factory: async_sessionmaker[AsyncSession], wrong: str) -> None:
    _, driver, _, derivation, request = await setup(factory)

    def corrupt(batch: EmbeddingBatch) -> EmbeddingBatch:
        if wrong == "profile":
            return replace(batch, profile_digest="0" * 64)
        if wrong == "receipt":
            return replace(batch, receipt_digest="0" * 64)
        item = batch.items[0]
        return replace(
            batch,
            items=(
                replace(item, input_digest="0" * 64) if wrong == "input" else replace(item, vector=(float("nan"),)),
            ),
        )

    driver.transform = corrupt
    with pytest.raises(ValueError, match="refused"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    assert (await derivation.inspect(notes.SUBJECT, request, purpose="status")).state == "failed"


async def test_retired_generation_replay_cannot_reactivate(factory: async_sessionmaker[AsyncSession]) -> None:
    _, _, _, derivation, request = await setup(factory)
    await derivation.retire(notes.SUBJECT, derivation.reference, expected_version=1, purpose="manage")
    assert (await derivation.register(notes.SUBJECT, derivation.reference, purpose="manage")).state == "retired"
    with pytest.raises(ArchiveRefused, match="generation_retired"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    async with factory() as session:
        assert len(list(await session.scalars(select(ArchiveEmbeddingGeneration)))) == 1


async def test_two_prepared_callers_close_losing_lease(factory: async_sessionmaker[AsyncSession]) -> None:
    _, driver, _, derivation, request = await setup(factory)
    driver.claim_release.clear()
    first = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.lease_entered.wait()
    driver.lease_entered.clear()
    second = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.lease_entered.wait()
    driver.claim_release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert driver.leases == driver.closed == 2
    assert driver.calls == 1
    assert sum(isinstance(result, ArchiveRefused) and result.code == "attempt_stale" for result in outcomes) == 1


async def test_cancellation_retains_explicit_expiring_attempt(factory: async_sessionmaker[AsyncSession]) -> None:
    _, driver, _, derivation, request = await setup(factory)
    driver.release.clear()
    work = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.entered.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert driver.closed == 1
    status = await derivation.inspect(notes.SUBJECT, request, purpose="status")
    assert status.state == "running"
    assert status.expires_at is not None


async def test_oversize_source_never_reaches_driver_or_changes_lexical_note(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, driver, archive, derivation, _ = await setup(factory)
    note = await notes.promote(archive, notes.proposal(source_key="large", text="z" * 4097))
    request = DerivationRequest(generation=derivation.reference, source=note.reference)
    with pytest.raises(ArchiveRefused, match="embedding_input_size"):
        await derivation.derive(notes.SUBJECT, request, purpose="derive")
    assert driver.calls == 0
    assert (await derivation.inspect(notes.SUBJECT, request, purpose="status")).state == "failed"
    receipt = await archive.recall(notes.SUBJECT, notes.query("z" * 20), purpose="answer")
    assert receipt.namespace_id == note.namespace_id


async def test_corrupt_binding_refuses_metadata_before_vector_hydration(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, _, derivation, request = await setup(factory)
    await derivation.derive(notes.SUBJECT, request, purpose="derive")
    async with factory() as session, session.begin():
        await session.execute(text("UPDATE archive_note_embedding SET binding_digest=repeat('0',64)"))
    with pytest.raises(ArchiveRefused, match="derivation_binding"):
        await derivation.inspect(notes.SUBJECT, request, purpose="status")


async def test_database_rejects_dimension_and_foreign_namespace_pairs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy.exc import DBAPIError

    _, _, _, derivation, request = await setup(factory)
    result = await derivation.derive(notes.SUBJECT, request, purpose="derive")
    with pytest.raises(DBAPIError, match="expected 1024 dimensions"):
        async with factory() as session, session.begin():
            await session.execute(text("UPDATE archive_note_embedding SET vector='[1,0]'"))
    foreign = notes.PROFILE.model_copy(update={"namespace_id": uuid4()})
    archive = notes.service(factory, notes.Authority(foreign))
    await archive.admit(notes.SUBJECT, notes.proposal(namespace_id=foreign.namespace_id), purpose="manage")
    with pytest.raises(DBAPIError, match="foreign key constraint"):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE archive_note_embedding SET namespace_id=:namespace"), {"namespace": foreign.namespace_id}
            )
    assert (await derivation.inspect(notes.SUBJECT, request, purpose="status")).receipt_digest == result.receipt_digest


async def test_waiting_claim_rechecks_current_authority(factory: async_sessionmaker[AsyncSession]) -> None:
    from lychd.db.models.archive import ArchiveNamespace

    authority, driver, _, derivation, request = await setup(factory)
    authority.called.clear()
    async with factory() as blocker, blocker.begin():
        await blocker.scalar(
            select(ArchiveNamespace).where(ArchiveNamespace.id == notes.PROFILE.namespace_id).with_for_update()
        )
        pending = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
        await authority.called.wait()
        authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        await pending
    assert driver.calls == driver.leases == 0


async def test_expired_completion_without_takeover_cannot_publish(factory: async_sessionmaker[AsyncSession]) -> None:
    _, driver, _, derivation, request = await setup(factory)
    driver.release.clear()
    pending = asyncio.create_task(derivation.derive(notes.SUBJECT, request, purpose="derive"))
    await driver.entered.wait()
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE archive_note_embedding SET expires_at=clock_timestamp()-interval '1 second'")
        )
    driver.release.set()
    with pytest.raises(ArchiveRefused, match="attempt_stale"):
        await pending
    status = await derivation.inspect(notes.SUBJECT, request, purpose="status")
    assert status.state == "expired"
    assert status.vector_digest is None
    assert status.receipt_digest is None
    assert driver.closed == 1
