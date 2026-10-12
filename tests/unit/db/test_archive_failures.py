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
