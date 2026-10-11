"""Real PostgreSQL admission, lifecycle, replay and exact-receipt recovery receipts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio

pytest.importorskip("testcontainers", reason="optional disposable PostgreSQL receipt")

from sqlalchemy import Table, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from lychd.db.archive import DbArchiveStore
from lychd.db.models.archive import ArchiveIntake, ArchiveNamespace, ArchiveNote, ArchiveRecall
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
        [ArchiveNamespace.__table__, ArchiveNote.__table__, ArchiveIntake.__table__, ArchiveRecall.__table__],
    )
    async with engine.begin() as connection:
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
        with pytest.raises(DBAPIError, match="lock timeout"):
            await archive.recall(SUBJECT, query(), purpose=PURPOSE)
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
    with pytest.raises(DBAPIError, match="statement timeout"):
        await service(slow_factory).recall(SUBJECT, query(), purpose=PURPOSE)
    assert observed == [("5s", "2s")]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0
        assert await session.scalar(text("SHOW statement_timeout")) == "0"
