"""Archive storage failures must not copy SQL source parameters into callers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from traceback import format_exception
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from sqlalchemy.exc import StatementError

from lychd.db.archive import DbArchiveStore
from lychd.db.models.archive import ArchiveNamespace, ArchiveRecall
from lychd.domain.archive import (
    ArchiveProfile,
    ArchiveRefused,
    ArchiveSubject,
    Lifecycle,
    NoteProposal,
    RecallReceipt,
    RecallRequest,
    text_digest,
)
from lychd.domain.archive.contracts import digest

SUBJECT = ArchiveSubject(identity="test-owner")
PROFILE = ArchiveProfile(namespace_id=UUID(int=1), owner_subject=SUBJECT, policy_revision="one")
SOURCE = "SOURCE_PARAMETER_MUST_NOT_REACH_FAILURE_HISTORY"


def allow_check(object_id: UUID | None, binding_digest: str) -> None:
    pass


async def _invoke(store: DbArchiveStore, operation: str) -> None:
    def check(*_: object) -> None:
        pass

    if operation == "admit":
        proposal = NoteProposal(
            namespace_id=PROFILE.namespace_id,
            request_id=UUID(int=2),
            source_key="note",
            source_revision="one",
            source_digest=text_digest(SOURCE),
            text=SOURCE,
            producer="test",
            observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        )
        await store.admit(PROFILE, proposal, check)
    elif operation == "transition":
        await store.transition(PROFILE, UUID(int=2), 1, Lifecycle.PROMOTED, check)
    elif operation == "recall":
        await store.recall(
            PROFILE,
            SUBJECT,
            RecallRequest(namespace_id=PROFILE.namespace_id, request_id=UUID(int=2), query=SOURCE),
            "answer",
            check,
        )
    else:
        await store.resolve(PROFILE, SUBJECT, UUID(int=2), "a" * 64, "answer", check)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["admit", "transition", "recall", "resolve"])
@pytest.mark.parametrize("failure", ["statement", "authority"])
async def test_archive_storage_failure_is_safe_and_still_rolls_back(
    operation: str, failure: Literal["statement", "authority"]
) -> None:
    error = (
        StatementError("database failed", "SELECT :content", {"content": SOURCE}, RuntimeError(SOURCE))
        if failure == "statement"
        else ArchiveRefused("authority")
    )
    session = AsyncMock()
    session.execute.side_effect = error
    rolled_back: list[BaseException] = []
    closed: list[bool] = []

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        try:
            yield
        except BaseException as caught:
            rolled_back.append(caught)
            raise

    session.begin = Mock(side_effect=transaction)

    @asynccontextmanager
    async def factory() -> AsyncGenerator[Any]:
        try:
            yield session
        finally:
            closed.append(True)

    store = DbArchiveStore(cast("Any", factory))
    with pytest.raises(ArchiveRefused) as caught:
        await _invoke(store, operation)
    assert caught.value.code == ("storage" if failure == "statement" else "authority")
    assert SOURCE not in str(caught.value)
    assert SOURCE not in repr(caught.value)
    assert SOURCE not in "".join(format_exception(caught.value))
    assert rolled_back == [error]
    assert closed == [True]
    if failure == "statement":
        assert caught.value.__suppress_context__ is True
    else:
        assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "commit", "close"])
@pytest.mark.parametrize("failure", ["statement", "authority", "cancelled"])
async def test_archive_transaction_lifetime_failures_remain_safe(
    phase: str, failure: Literal["statement", "authority", "cancelled"]
) -> None:
    """A successful replay must still classify faults outside the SQL body."""
    request = RecallRequest(namespace_id=PROFILE.namespace_id, request_id=UUID(int=2), query=SOURCE)
    request_digest = digest(
        {
            "request": request.model_dump(mode="json"),
            "subject": SUBJECT.model_dump(),
            "purpose": "answer",
            "profile": PROFILE.model_dump(mode="json"),
        }
    )
    receipt = RecallReceipt(
        receipt_id=UUID(int=3),
        request_id=request.request_id,
        namespace_id=PROFILE.namespace_id,
        subject=SUBJECT,
        purpose="answer",
        policy_revision=PROFILE.policy_revision,
        query_digest=text_digest(SOURCE),
        request_digest=request_digest,
        run_id=None,
        max_results=request.max_results,
        char_budget=request.char_budget,
        min_score=request.min_score,
        selected=(),
        omitted_for_budget=0,
        created_at=datetime(2026, 10, 12, tzinfo=UTC),
        receipt_digest="0" * 64,
    )
    receipt = receipt.model_copy(update={"receipt_digest": receipt.computed_digest()})
    session = AsyncMock()
    session.scalar.side_effect = [
        ArchiveNamespace(
            id=PROFILE.namespace_id,
            owner_subject=SUBJECT.identity,
            policy_revision=PROFILE.policy_revision,
        ),
        ArchiveRecall(
            id=receipt.receipt_id,
            namespace_id=PROFILE.namespace_id,
            request_id=request.request_id,
            request_digest=request_digest,
            receipt_digest=receipt.receipt_digest,
            receipt=receipt.model_dump(mode="json"),
        ),
    ]
    errors = {
        "statement": StatementError("database failed", "SELECT :content", {"content": SOURCE}, RuntimeError(SOURCE)),
        "authority": ArchiveRefused("authority"),
        "cancelled": asyncio.CancelledError(),
    }
    error = errors[failure]
    closed: list[bool] = []

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        if phase == "begin":
            raise error
        yield
        if phase == "commit":
            raise error

    session.begin = Mock(side_effect=transaction)

    @asynccontextmanager
    async def factory() -> AsyncGenerator[Any]:
        try:
            yield session
        finally:
            closed.append(True)
            if phase == "close":
                raise error

    store = DbArchiveStore(cast("Any", factory))
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else ArchiveRefused) as caught:
        await _invoke(store, "recall")
    assert closed == [True]
    if failure == "statement":
        assert isinstance(caught.value, ArchiveRefused)
        assert caught.value.code == "storage"
        assert caught.value.__suppress_context__ is True
        assert SOURCE not in "".join(format_exception(caught.value))
    else:
        assert caught.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list_notes", "get_note"])
async def test_metadata_storage_failures_are_safe(operation: str) -> None:
    from lychd.domain.archive import NoteListRequest

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    session.execute.side_effect = StatementError("failed", "SELECT :content", {"content": SOURCE}, RuntimeError(SOURCE))
    store = DbArchiveStore(cast("Any", lambda: session))
    arg = NoteListRequest(namespace_id=PROFILE.namespace_id) if operation == "list_notes" else UUID(int=2)
    with pytest.raises(ArchiveRefused, match="storage") as caught:
        await getattr(store, operation)(PROFILE, arg, allow_check)
    assert SOURCE not in "".join(format_exception(caught.value))


@pytest.mark.asyncio
async def test_metadata_missing_namespace_performs_no_persistent_writes() -> None:
    from lychd.domain.archive import NoteListRequest

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    session.scalar.return_value = None
    store = DbArchiveStore(cast("Any", lambda: session))
    checks: list[tuple[UUID | None, str]] = []

    def check(object_id: UUID | None, binding_digest: str) -> None:
        checks.append((object_id, binding_digest))

    assert (await store.list_notes(PROFILE, NoteListRequest(namespace_id=PROFILE.namespace_id), check)).records == ()
    with pytest.raises(ArchiveRefused, match="record_missing"):
        await store.get_note(PROFILE, UUID(int=2), check)
    assert len(checks) == 2
    assert all(str(call.args[0]).startswith("SET LOCAL") for call in session.execute.await_args_list)
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["owner_subject", "policy_revision"])
async def test_metadata_mismatched_namespace_binding_is_not_empty(field: str) -> None:
    from lychd.domain.archive import NoteListRequest

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    values = {
        "id": PROFILE.namespace_id,
        "owner_subject": SUBJECT.identity,
        "policy_revision": PROFILE.policy_revision,
        field: "different",
    }
    session.scalar.return_value = ArchiveNamespace(**values)
    store = DbArchiveStore(cast("Any", lambda: session))
    with pytest.raises(ArchiveRefused, match="namespace_binding"):
        await store.list_notes(PROFILE, NoteListRequest(namespace_id=PROFILE.namespace_id), allow_check)


@pytest.mark.asyncio
async def test_metadata_selects_only_bounded_columns_and_preserves_ties() -> None:
    from types import SimpleNamespace

    from lychd.domain.archive import NoteListCursor, NoteListRequest

    instant = datetime(2026, 10, 19, 1, 2, 3, 123456, tzinfo=UTC)
    rows = [
        {
            "namespace_id": PROFILE.namespace_id,
            "id": UUID(int=index),
            "revision_digest": "1" * 64,
            "lifecycle_version": 3,
            "source_key": "fox",
            "source_revision": str(index),
            "source_digest": "2" * 64,
            "producer": "operator",
            "observed_at": instant,
            "lifecycle": "revoked",
            "created_at": instant,
        }
        for index in (3, 2, 1)
    ]
    statements: list[Any] = []

    async def execute(statement: Any) -> Any:
        if not str(statement).startswith("SET LOCAL"):
            statements.append(statement)
        before = str(statement.compile(compile_kwargs={"literal_binds": True}))
        selected = rows[2:] if "archive_note.id <" in before else rows
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: selected, one_or_none=lambda: rows[0]))

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    session.execute.side_effect = execute
    session.scalar.return_value = ArchiveNamespace(
        id=PROFILE.namespace_id, owner_subject=SUBJECT.identity, policy_revision=PROFILE.policy_revision
    )
    store = DbArchiveStore(cast("Any", lambda: session))
    first = await store.list_notes(
        PROFILE, NoteListRequest(namespace_id=PROFILE.namespace_id, source_key="fox", limit=2), allow_check
    )
    assert first.next_cursor is not None
    assert NoteListCursor.decode(first.next_cursor).created_at == instant
    second = await store.list_notes(
        PROFILE,
        NoteListRequest(namespace_id=PROFILE.namespace_id, source_key="fox", limit=2, cursor=first.next_cursor),
        allow_check,
    )
    assert [item.reference.record_id.int for item in (*first.records, *second.records)] == [3, 2, 1]
    assert second.next_cursor is None
    assert (await store.get_note(PROFILE, UUID(int=3), allow_check)).lifecycle is Lifecycle.REVOKED
    for statement in statements:
        assert {column.name for column in statement.selected_columns} == {
            "namespace_id",
            "id",
            "revision_digest",
            "lifecycle_version",
            "source_key",
            "source_revision",
            "source_digest",
            "producer",
            "observed_at",
            "lifecycle",
            "created_at",
        }
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "content" not in sql
        assert "search_vector" not in sql
        assert "embedding" not in sql
    assert "LIMIT 3" in str(statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "archive_note.created_at <" in str(statements[1])
    session.add.assert_not_called()
