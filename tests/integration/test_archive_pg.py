"""Real PostgreSQL admission, lifecycle, replay and exact-receipt recovery receipts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers", reason="optional disposable PostgreSQL receipt")

from sqlalchemy import Table, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from lychd.db.archive import DbArchiveStore
from lychd.db.models.archive import ArchiveIntake, ArchiveNamespace, ArchiveNote, ArchiveRecall
from lychd.db.models.archive_derivation import ArchiveEmbeddingGeneration, ArchiveNoteEmbedding
from lychd.domain.archive import (
    ArchiveAuthorization,
    ArchiveProfile,
    ArchiveRefused,
    ArchiveService,
    ArchiveSubject,
    Lifecycle,
    NoteProposal,
    NoteRecord,
    RecallRequest,
    text_digest,
)

pytestmark = [pytest.mark.integration, pytest.mark.container]
SUBJECT = ArchiveSubject(identity="local-operator")
PROFILE = ArchiveProfile(namespace_id=uuid4(), owner_subject=SUBJECT, policy_revision="public-notes.1")
PURPOSE = "answer-public-note"


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("pgvector/pgvector:pg18-trixie", driver="asyncpg") as postgres:
        yield postgres.get_connection_url()


@pytest_asyncio.fixture
async def factory(pg_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_url)
    tables = cast(
        "list[Table]",
        [
            ArchiveNamespace.__table__,
            ArchiveNote.__table__,
            ArchiveIntake.__table__,
            ArchiveRecall.__table__,
            ArchiveEmbeddingGeneration.__table__,
            ArchiveNoteEmbedding.__table__,
        ],
    )
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(ArchiveNote.metadata.drop_all, tables=tables)
        await connection.run_sync(ArchiveNote.metadata.create_all, tables=tables)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Authority:
    def __init__(self, profile: ArchiveProfile = PROFILE) -> None:
        self.profile = profile
        self.allowed = True
        self.calls = 0
        self.deny_at: int | None = None
        self.called = asyncio.Event()

    def authorize(self, request: ArchiveAuthorization) -> bool:
        self.calls += 1
        self.called.set()
        return (
            self.allowed
            and self.calls != self.deny_at
            and request.subject == self.profile.owner_subject
            and request.namespace_id == self.profile.namespace_id
            and request.policy_revision == self.profile.policy_revision
        )


def service(factory: async_sessionmaker[AsyncSession], authority: Authority | None = None) -> ArchiveService:
    authorizer = authority or Authority()
    return ArchiveService(profile=authorizer.profile, store=DbArchiveStore(factory), authorizer=authorizer)


def proposal(**updates: Any) -> NoteProposal:
    content = updates.pop("text", "A red fox guards the archive.")
    values = {
        "namespace_id": PROFILE.namespace_id,
        "request_id": uuid4(),
        "source_key": "operator-note",
        "source_revision": "1",
        "source_digest": text_digest(content),
        "text": content,
        "producer": "operator",
        "observed_at": datetime(2026, 10, 11, tzinfo=UTC),
    }
    values.update(updates)
    return NoteProposal.model_validate(values)


def query(text: str = "fox", **updates: Any) -> RecallRequest:
    return RecallRequest.model_validate(
        {"namespace_id": PROFILE.namespace_id, "request_id": uuid4(), "query": text, **updates}
    )


async def promote(archive: ArchiveService, note: NoteProposal | None = None) -> NoteRecord:
    candidate = await archive.admit(SUBJECT, note or proposal(), purpose=PURPOSE)
    return await archive.promote(
        SUBJECT, candidate.reference.record_id, expected_version=candidate.reference.lifecycle_version, purpose=PURPOSE
    )


@pytest.mark.asyncio
async def test_intake_concurrency_exact_replay_and_conflicts(factory: async_sessionmaker[AsyncSession]) -> None:
    archive = service(factory)
    note = proposal()
    first, second = await asyncio.gather(
        archive.admit(SUBJECT, note, purpose=PURPOSE), archive.admit(SUBJECT, note, purpose=PURPOSE)
    )
    assert first == second
    duplicate_source = await archive.admit(SUBJECT, note.model_copy(update={"request_id": uuid4()}), purpose=PURPOSE)
    assert duplicate_source.reference == first.reference
    with pytest.raises(ArchiveRefused, match="request_conflict"):
        await archive.admit(SUBJECT, proposal(request_id=note.request_id, text="blue whale"), purpose=PURPOSE)
    with pytest.raises(ArchiveRefused, match="source_conflict"):
        await archive.admit(SUBJECT, proposal(text="blue whale"), purpose=PURPOSE)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveNote)) == 1
        assert await session.scalar(select(func.count()).select_from(ArchiveIntake)) == 2


@pytest.mark.asyncio
async def test_candidate_promotion_restart_receipt_has_no_raw_text(factory: async_sessionmaker[AsyncSession]) -> None:
    archive = service(factory)
    candidate = await archive.admit(SUBJECT, proposal(), purpose=PURPOSE)
    miss = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    assert miss.selected == ()
    await archive.promote(SUBJECT, candidate.reference.record_id, expected_version=1, purpose=PURPOSE)
    request = query("red fox")
    receipt = await archive.recall(SUBJECT, request, purpose=PURPOSE)
    assert len(receipt.selected) == 1
    assert receipt == await archive.recall(SUBJECT, request, purpose=PURPOSE)
    rebooted = service(factory)
    projection = await rebooted.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)
    assert projection.items[0].text == proposal().text
    assert request.query not in receipt.model_dump_json()
    assert proposal().text not in receipt.model_dump_json()
    with pytest.raises(ArchiveRefused, match="receipt_binding"):
        await rebooted.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose="another-purpose")


@pytest.mark.asyncio
async def test_correction_is_staged_then_atomically_supersedes(factory: async_sessionmaker[AsyncSession]) -> None:
    archive = service(factory)
    first = await promote(archive)
    old_receipt = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    correction = await archive.admit(
        SUBJECT, proposal(source_revision="2", text="A blue fox guards the archive."), purpose=PURPOSE
    )
    still_old = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    assert still_old.selected[0].reference == first.reference
    await archive.promote(SUBJECT, correction.reference.record_id, expected_version=1, purpose=PURPOSE)
    current = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    assert current.selected[0].reference.record_id == correction.reference.record_id
    with pytest.raises(ArchiveRefused, match="receipt_stale"):
        await archive.resolve_receipt(SUBJECT, old_receipt.receipt_id, old_receipt.receipt_digest, purpose=PURPOSE)
    async with factory() as session:
        previous = await session.get(ArchiveNote, first.reference.record_id)
        assert previous is not None
        assert previous.lifecycle == Lifecycle.ARCHIVED
        assert previous.lifecycle_version == 3


@pytest.mark.asyncio
async def test_revocation_erases_text_search_and_prevents_reingestion(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    archive = service(factory)
    note = proposal()
    promoted = await promote(archive, note)
    receipt = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    revoked = await archive.revoke(SUBJECT, promoted.reference.record_id, expected_version=2, purpose=PURPOSE)
    assert revoked.lifecycle == Lifecycle.REVOKED
    assert (await archive.recall(SUBJECT, query(), purpose=PURPOSE)).selected == ()
    for replay in (note, note.model_copy(update={"request_id": uuid4()})):
        with pytest.raises(ArchiveRefused, match="revoked"):
            await archive.admit(SUBJECT, replay, purpose=PURPOSE)
    with pytest.raises(ArchiveRefused, match="receipt_stale"):
        await service(factory).resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)
    async with factory() as session:
        stored = await session.get(ArchiveNote, promoted.reference.record_id)
        assert stored is not None
        assert stored.content is None
        assert str(stored.search_vector) == ""
        assert stored.source_digest == note.source_digest
        assert await session.get(ArchiveRecall, receipt.receipt_id) is not None


@pytest.mark.asyncio
async def test_lexical_bounds_threshold_ties_and_namespace(factory: async_sessionmaker[AsyncSession]) -> None:
    archive = service(factory)
    records = [await promote(archive, proposal(source_key=f"note-{index}", text="red fox")) for index in range(3)]
    receipt = await archive.recall(SUBJECT, query(max_results=2), purpose=PURPOSE)
    assert [item.reference.record_id for item in receipt.selected] == sorted(
        item.reference.record_id for item in records
    )[:2]
    budget = await archive.recall(SUBJECT, query(char_budget=6), purpose=PURPOSE)
    assert budget.selected == ()
    assert budget.omitted_for_budget == 3
    assert (await archive.recall(SUBJECT, query(min_score=1.0), purpose=PURPOSE)).selected == ()
    assert (await archive.recall(SUBJECT, query("missing"), purpose=PURPOSE)).selected == ()
    other = PROFILE.model_copy(update={"namespace_id": uuid4()})
    other_archive = service(factory, Authority(other))
    assert (await other_archive.recall(SUBJECT, query(namespace_id=other.namespace_id), purpose=PURPOSE)).selected == ()
    with pytest.raises(ArchiveRefused, match="namespace"):
        await archive.recall(SUBJECT, query(namespace_id=other.namespace_id), purpose=PURPOSE)
    with pytest.raises(ArchiveRefused, match="receipt_missing"):
        await other_archive.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)


@pytest.mark.asyncio
async def test_current_authority_rechecked_after_namespace_wait(factory: async_sessionmaker[AsyncSession]) -> None:
    authority = Authority()
    archive = service(factory, authority)
    await promote(archive)
    async with factory() as session, session.begin():
        await session.scalar(
            select(ArchiveNamespace).where(ArchiveNamespace.id == PROFILE.namespace_id).with_for_update()
        )
        authority.called.clear()
        task = asyncio.create_task(archive.recall(SUBJECT, query(), purpose=PURPOSE))
        await asyncio.wait_for(authority.called.wait(), timeout=5)
        authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        await task
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0


@pytest.mark.asyncio
async def test_final_authority_refusal_rolls_back_intake(factory: async_sessionmaker[AsyncSession]) -> None:
    authority = Authority()
    authority.deny_at = 5  # initial, post-lock, new note, intake, after flush
    with pytest.raises(ArchiveRefused, match="authority"):
        await service(factory, authority).admit(SUBJECT, proposal(), purpose=PURPOSE)
    async with factory() as session:
        for row_type in (ArchiveNote, ArchiveIntake, ArchiveNamespace):
            assert await session.scalar(select(func.count()).select_from(row_type)) == 0


@pytest.mark.asyncio
async def test_concurrent_lifecycle_has_one_winner_and_stale_receipt_refuses(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    archive = service(factory)
    promoted = await promote(archive)
    receipt = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    results = await asyncio.gather(
        archive.archive(SUBJECT, promoted.reference.record_id, expected_version=2, purpose=PURPOSE),
        archive.revoke(SUBJECT, promoted.reference.record_id, expected_version=2, purpose=PURPOSE),
        return_exceptions=True,
    )
    assert sum(isinstance(item, NoteRecord) for item in results) == 1
    assert sum(isinstance(item, ArchiveRefused) and item.code == "stale_version" for item in results) == 1
    with pytest.raises(ArchiveRefused, match="receipt_stale"):
        await archive.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)


@pytest.mark.asyncio
async def test_restart_changed_policy_or_owner_refuses_retained_namespace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    original = service(factory)
    await promote(original)
    receipt = await original.recall(SUBJECT, query(), purpose=PURPOSE)
    for changed in (
        PROFILE.model_copy(update={"policy_revision": "public-notes.2"}),
        PROFILE.model_copy(update={"owner_subject": ArchiveSubject(identity="another-local-owner")}),
    ):
        reopened = service(factory, Authority(changed))
        with pytest.raises(ArchiveRefused, match="namespace_binding"):
            await reopened.resolve_receipt(
                changed.owner_subject, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE
            )
    authority = Authority()
    authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        await service(factory, authority).resolve_receipt(
            SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE
        )


@pytest.mark.asyncio
async def test_receipt_content_handoff_rechecks_authority_after_real_session_exit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    archive = service(factory)
    await promote(archive)
    receipt = await archive.recall(SUBJECT, query(), purpose=PURPOSE)
    authority = Authority()

    class RevokingSession(AsyncSession):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            authority.allowed = False

    revoking_factory = async_sessionmaker[AsyncSession](
        factory.kw["bind"], class_=RevokingSession, expire_on_commit=False
    )
    reopened = service(revoking_factory, authority)
    with pytest.raises(ArchiveRefused, match="authority"):
        await reopened.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)
    assert authority.allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["admit", "promote", "archive", "revoke", "recall"])
async def test_revoked_response_does_not_claim_committed_operation_rolled_back(
    factory: async_sessionmaker[AsyncSession], action: str
) -> None:
    archive = service(factory)
    note = proposal()
    record = None
    if action != "admit":
        record = await archive.admit(SUBJECT, note, purpose=PURPOSE)
    if action in {"archive", "revoke", "recall"}:
        assert record is not None
        record = await archive.promote(SUBJECT, record.reference.record_id, expected_version=1, purpose=PURPOSE)
    authority = Authority()

    class RevokingSession(AsyncSession):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            authority.allowed = False

    revoking_factory = async_sessionmaker[AsyncSession](
        factory.kw["bind"], class_=RevokingSession, expire_on_commit=False
    )
    revoking = service(revoking_factory, authority)
    request = query()
    if action == "admit":
        pending = revoking.admit(SUBJECT, note, purpose=PURPOSE)
    elif action == "recall":
        pending = revoking.recall(SUBJECT, request, purpose=PURPOSE)
    else:
        assert record is not None
        operation = {"promote": revoking.promote, "archive": revoking.archive, "revoke": revoking.revoke}[action]
        pending = operation(
            SUBJECT, record.reference.record_id, expected_version=record.reference.lifecycle_version, purpose=PURPOSE
        )
    with pytest.raises(ArchiveRefused, match="authority"):
        await pending
    assert not authority.allowed
    async with factory() as session:
        retained = await session.scalar(select(ArchiveNote))
        assert retained is not None
        expected = {
            "admit": "candidate",
            "promote": "promoted",
            "archive": "archived",
            "revoke": "revoked",
            "recall": "promoted",
        }
        assert retained.lifecycle == expected[action]
    authority.allowed = True
    reopened = service(factory, authority)
    from lychd.domain.archive import NoteListRequest

    recovered = await reopened.list_notes(SUBJECT, NoteListRequest(namespace_id=PROFILE.namespace_id), purpose=PURPOSE)
    assert recovered.records[0].lifecycle == expected[action]
    assert (
        await reopened.get_note(SUBJECT, recovered.records[0].reference.record_id, purpose=PURPOSE)
        == recovered.records[0]
    )
    if action == "recall":
        replay = await reopened.recall(SUBJECT, request, purpose=PURPOSE)
        assert len(replay.selected) == 1
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 1
    elif action != "revoke":
        replay_record = await reopened.admit(SUBJECT, note, purpose=PURPOSE)
        assert replay_record.lifecycle == expected[action]
        if action != "admit":
            assert record is not None
            with pytest.raises(ArchiveRefused, match="stale_version"):
                await reopened.revoke(
                    SUBJECT,
                    record.reference.record_id,
                    expected_version=record.reference.lifecycle_version,
                    purpose=PURPOSE,
                )
    else:
        with pytest.raises(ArchiveRefused, match="revoked"):
            await reopened.admit(SUBJECT, note, purpose=PURPOSE)


@pytest.mark.asyncio
async def test_namespace_lock_timeout_is_failure_and_rolls_back_request(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    archive = service(factory)
    await promote(archive)
    async with factory() as session, session.begin():
        await session.scalar(
            select(ArchiveNamespace).where(ArchiveNamespace.id == PROFILE.namespace_id).with_for_update()
        )
        with pytest.raises(ArchiveRefused, match="storage") as failure:
            await archive.recall(SUBJECT, query(), purpose=PURPOSE)
        assert failure.value.__suppress_context__ is True
        assert isinstance(failure.value.__context__, DBAPIError)
        assert "lock timeout" in str(failure.value.__context__)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0
        assert await session.scalar(text("SHOW lock_timeout")) == "0"


@pytest.mark.asyncio
async def test_lexical_statement_timeout_is_local_failure_not_a_miss(factory: async_sessionmaker[AsyncSession]) -> None:
    archive = service(factory)
    await promote(archive)
    observed: list[tuple[str, str]] = []

    class SlowRecallSession(AsyncSession):
        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "ts_rank_cd" in str(statement):
                settings = await super().execute(
                    text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
                )
                setting_row = settings.one()
                observed.append((str(setting_row[0]), str(setting_row[1])))
                await super().execute(text("SELECT pg_sleep(10)"))
            return await super().execute(statement, *args, **kwargs)

    slow_factory = async_sessionmaker[AsyncSession](
        factory.kw["bind"], class_=SlowRecallSession, expire_on_commit=False
    )
    with pytest.raises(ArchiveRefused, match="storage") as failure:
        await service(slow_factory).recall(SUBJECT, query(), purpose=PURPOSE)
    assert failure.value.__suppress_context__ is True
    assert isinstance(failure.value.__context__, DBAPIError)
    assert "statement timeout" in str(failure.value.__context__)
    assert observed == [("5s", "2s")]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0
        assert await session.scalar(text("SHOW statement_timeout")) == "0"


@pytest.mark.asyncio
async def test_metadata_reload_across_lifecycles_and_missing_namespace_without_writes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from lychd.domain.archive import NoteListRequest

    archive = service(factory)
    assert await archive.discover_profile(SUBJECT, purpose=PURPOSE) == PROFILE
    assert (
        await archive.list_notes(SUBJECT, NoteListRequest(namespace_id=PROFILE.namespace_id), purpose=PURPOSE)
    ).records == ()
    with pytest.raises(ArchiveRefused, match="record_missing"):
        await archive.get_note(SUBJECT, uuid4(), purpose=PURPOSE)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveNamespace)) == 0
        assert await session.scalar(select(func.count()).select_from(ArchiveIntake)) == 0
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0
    first = await archive.admit(SUBJECT, proposal(), purpose=PURPOSE)
    assert (
        await service(factory).get_note(SUBJECT, first.reference.record_id, purpose=PURPOSE)
    ).lifecycle is Lifecycle.CANDIDATE
    await archive.promote(SUBJECT, first.reference.record_id, expected_version=1, purpose=PURPOSE)
    assert (
        await service(factory).get_note(SUBJECT, first.reference.record_id, purpose=PURPOSE)
    ).lifecycle is Lifecycle.PROMOTED
    correction = await archive.admit(
        SUBJECT, proposal(source_revision="2", text="A blue fox guards the archive."), purpose=PURPOSE
    )
    await archive.promote(SUBJECT, correction.reference.record_id, expected_version=1, purpose=PURPOSE)
    await archive.revoke(SUBJECT, correction.reference.record_id, expected_version=2, purpose=PURPOSE)
    candidate = await archive.admit(SUBJECT, proposal(source_key="other"), purpose=PURPOSE)
    # Force timestamp ties and reconstruct the service to exercise retained keysets.
    instant = datetime(2026, 10, 19, 1, 2, 3, 123456, tzinfo=UTC)
    async with factory() as session, session.begin():
        for row in await session.scalars(select(ArchiveNote)):
            row.created_at = instant
    fresh = service(factory)
    ids: list[UUID] = []
    cursor = None
    for _ in range(4):
        page = await fresh.list_notes(
            SUBJECT, NoteListRequest(namespace_id=PROFILE.namespace_id, limit=1, cursor=cursor), purpose=PURPOSE
        )
        ids.extend(item.reference.record_id for item in page.records)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    else:
        pytest.fail("Metadata pagination did not terminate within its retained rows.")
    assert ids == sorted(
        [first.reference.record_id, correction.reference.record_id, candidate.reference.record_id], reverse=True
    )
    assert (await fresh.get_note(SUBJECT, first.reference.record_id, purpose=PURPOSE)).lifecycle is Lifecycle.ARCHIVED
    assert (
        await fresh.get_note(SUBJECT, correction.reference.record_id, purpose=PURPOSE)
    ).lifecycle is Lifecycle.REVOKED
    selected = await fresh.list_notes(
        SUBJECT, NoteListRequest(namespace_id=PROFILE.namespace_id, source_key="operator-note"), purpose=PURPOSE
    )
    assert len(selected.records) == 2
    assert "content" not in selected.model_dump_json()
    assert "text" not in selected.model_dump_json()
    with pytest.raises(ArchiveRefused, match="revoked"):
        await fresh.admit(
            SUBJECT, proposal(source_revision="2", text="A blue fox guards the archive."), purpose=PURPOSE
        )
    with pytest.raises(ArchiveRefused, match="stale_version"):
        await fresh.revoke(SUBJECT, first.reference.record_id, expected_version=1, purpose=PURPOSE)
    for updates in ({"owner_subject": ArchiveSubject(identity="different")}, {"policy_revision": "different"}):
        changed = PROFILE.model_copy(update=updates)
        shifted = service(factory, Authority(changed))
        with pytest.raises(ArchiveRefused, match="namespace_binding"):
            await shifted.list_notes(
                changed.owner_subject, NoteListRequest(namespace_id=PROFILE.namespace_id), purpose=PURPOSE
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list_notes", "get_note"])
async def test_metadata_authority_revoked_after_initial_authorization(
    factory: async_sessionmaker[AsyncSession], operation: str
) -> None:
    from lychd.domain.archive import NoteListRequest

    retained = await promote(service(factory))
    authority = Authority()
    archive = service(factory, authority)
    arg = (
        NoteListRequest(namespace_id=PROFILE.namespace_id)
        if operation == "list_notes"
        else retained.reference.record_id
    )
    async with factory() as session, session.begin():
        await session.scalar(
            select(ArchiveNamespace).where(ArchiveNamespace.id == PROFILE.namespace_id).with_for_update()
        )
        task = asyncio.create_task(getattr(archive, operation)(SUBJECT, arg, purpose=PURPOSE))
        await asyncio.wait_for(authority.called.wait(), timeout=5)
        authority.allowed = False
    with pytest.raises(ArchiveRefused, match="authority"):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list_notes", "get_note"])
async def test_metadata_authority_revoked_at_session_cleanup(
    factory: async_sessionmaker[AsyncSession], operation: str
) -> None:
    from lychd.domain.archive import NoteListRequest

    retained = await promote(service(factory))
    authority = Authority()

    class RevokingSession(AsyncSession):
        async def close(self) -> None:
            await super().close()
            authority.allowed = False

    closing = async_sessionmaker[AsyncSession](factory.kw["bind"], class_=RevokingSession, expire_on_commit=False)
    archive = service(closing, authority)
    arg = (
        NoteListRequest(namespace_id=PROFILE.namespace_id)
        if operation == "list_notes"
        else retained.reference.record_id
    )
    with pytest.raises(ArchiveRefused, match="authority"):
        await getattr(archive, operation)(SUBJECT, arg, purpose=PURPOSE)


@pytest.mark.asyncio
async def test_historical_receipt_read_is_source_free_and_stable_across_lifecycle(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import event

    archive = service(factory)
    request = query(run_id=uuid4())
    assert await archive.inspect_recall(SUBJECT, request, recall_purpose=PURPOSE, purpose="archive:manage") is None
    async with factory() as session:
        for model in (ArchiveNamespace, ArchiveIntake, ArchiveRecall, ArchiveNote):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    first = await promote(archive)
    receipt = await archive.recall(SUBJECT, request, purpose=PURPOSE)
    assert receipt.selected
    observed: list[str] = []

    def observe(*args: Any) -> None:
        observed.append(str(args[2]).lower())

    async def read() -> None:
        event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", observe)
        try:
            fresh = service(factory)
            assert (
                await fresh.inspect_recall(SUBJECT, request, recall_purpose=PURPOSE, purpose="archive:manage")
                == receipt
            )
        finally:
            event.remove(factory.kw["bind"].sync_engine, "before_cursor_execute", observe)
        assert all("archive_note" not in statement for statement in observed)
        assert all(
            "insert " not in statement and "update " not in statement and "delete " not in statement
            for statement in observed
        )

    await read()
    corrected = await promote(archive, proposal(source_revision="2", text="A blue fox guards the archive."))
    await read()  # Promotion superseded the selected historical revision.
    await archive.archive(SUBJECT, corrected.reference.record_id, expected_version=2, purpose=PURPOSE)
    await read()
    await archive.revoke(SUBJECT, first.reference.record_id, expected_version=3, purpose=PURPOSE)
    await read()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 1
        assert await session.scalar(select(func.count()).select_from(ArchiveNamespace)) == 1
        assert await session.scalar(select(func.count()).select_from(ArchiveNote)) == 2
        assert await session.scalar(select(func.count()).select_from(ArchiveIntake)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparation", "cleanup"])
async def test_historical_authority_revoked_at_actual_awaited_boundaries(
    factory: async_sessionmaker[AsyncSession],
    phase: str,
) -> None:
    archive = service(factory)
    await promote(archive)
    request = query(run_id=uuid4())
    await archive.recall(SUBJECT, request, purpose=PURPOSE)
    authority = Authority()
    if phase == "cleanup":

        class RevokingSession(AsyncSession):
            async def close(self) -> None:
                await super().close()
                authority.allowed = False

        closing = async_sessionmaker[AsyncSession](factory.kw["bind"], class_=RevokingSession, expire_on_commit=False)
        with pytest.raises(ArchiveRefused, match="authority"):
            await service(closing, authority).inspect_recall(
                SUBJECT, request, recall_purpose=PURPOSE, purpose="archive:manage"
            )
    else:
        async with factory() as session, session.begin():
            await session.scalar(
                select(ArchiveNamespace).where(ArchiveNamespace.id == PROFILE.namespace_id).with_for_update()
            )
            task = asyncio.create_task(
                service(factory, authority).inspect_recall(
                    SUBJECT, request, recall_purpose=PURPOSE, purpose="archive:manage"
                )
            )
            await asyncio.wait_for(authority.called.wait(), timeout=5)
            authority.allowed = False
        with pytest.raises(ArchiveRefused, match="authority"):
            await task


@pytest.mark.asyncio
async def test_public_relevance_corpus_lexical_calibration(
    factory: async_sessionmaker[AsyncSession], record_testsuite_property: Callable[[str, object], None]
) -> None:
    # Heldout bytes are deliberately absent; this case cannot evaluate semantic quality.
    fixture_path = Path(__file__).parents[1] / "fixtures/archive/semantic-recall-need-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    notes = fixture["notes"]
    queries = fixture["calibration_queries"]
    assert len(notes) == 24
    assert len({note["source_key"] for note in notes}) == 24
    assert len(queries) == 12
    assert {
        kind: sum(item["query_kind"] == kind for item in queries) for kind in ("literal", "paraphrase", "unanswerable")
    } == {"literal": 4, "paraphrase": 4, "unanswerable": 4}
    assert all(item["split"] == "calibration" for item in queries)
    assert {item["language"] for item in queries} == {"en", "sk"}
    archive = service(factory)
    retained: dict[str, NoteRecord] = {}
    for note in notes:
        assert note["active"] is True
        assert note["revision"] == "1"
        assert text_digest(note["content"]) == note["content_sha256"]
        retained[note["source_key"]] = await promote(
            archive, proposal(source_key=note["source_key"], text=note["content"])
        )
    observations: list[dict[str, Any]] = []
    for item in queries:
        assert text_digest(item["text"]) == item["text_sha256"]
        relevant = set(item["relevant_source_keys"])
        assert relevant <= retained.keys()
        assert bool(relevant) is (item["query_kind"] != "unanswerable")
        request = query(item["text"], max_results=3, char_budget=32_000)
        start = perf_counter_ns()
        receipt = await archive.recall(SUBJECT, request, purpose=PURPOSE)
        elapsed_ns = perf_counter_ns() - start
        assert receipt.retrieval_policy == "postgres-simple-lexical@1"
        assert receipt.query_digest == item["text_sha256"]
        assert receipt.request_digest == request.binding_digest(PROFILE, SUBJECT, PURPOSE)
        assert receipt.computed_digest() == receipt.receipt_digest
        assert receipt.omitted_for_budget == 0
        projection = await archive.resolve_receipt(SUBJECT, receipt.receipt_id, receipt.receipt_digest, purpose=PURPOSE)
        selected = [entry.source_key for entry in receipt.selected]
        assert projection.receipt == receipt
        assert [entry.selection for entry in projection.items] == list(receipt.selected)
        for projected in projection.items:
            source = next(note for note in notes if note["source_key"] == projected.selection.source_key)
            assert projected.selection.reference == retained[source["source_key"]].reference
            assert projected.selection.source_revision == source["revision"]
            assert projected.selection.source_digest == source["content_sha256"]
            assert projected.selection.projection_digest == source["content_sha256"]
            assert projected.selection.projected_chars == len(source["content"])
            assert projected.text == source["content"]
        rank = next((rank for rank, key in enumerate(selected, 1) if key in relevant), None)
        observations.append(
            {
                "query_id": item["query_id"],
                "kind": item["query_kind"],
                "language": item["language"],
                "recall_at_3": len(set(selected) & relevant) / len(relevant) if relevant else None,
                "reciprocal_rank": 1 / rank if rank is not None else 0.0,
                "unanswerable_false_positive": bool(selected) if not relevant else None,
                "retrieval_elapsed_ns": elapsed_ns,
                "request": request.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
            }
        )
    record_testsuite_property("lexical_calibration", json.dumps(observations, ensure_ascii=False, sort_keys=True))
    assert all(item["recall_at_3"] == 1 for item in observations if item["kind"] == "literal")
    quartz = next(item for item in observations if item["query_id"] == "cal-en-quartz-paraphrase")
    assert quartz["recall_at_3"] == 0
    assert quartz["receipt"]["selected"] == []
