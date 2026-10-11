"""Portable DB adapter lock/transaction controls, without a PostgreSQL claim."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from lychd.db.delegation import DbDelegatedAgentJobStore
from lychd.db.models import DelegatedAgentEventRecord, DelegatedAgentJobRecord
from lychd.domain.delegation.models import (
    TERMINAL_DELEGATED_AGENT_STATUSES,
    DelegatedAgentEvent,
    DelegatedAgentEventKind,
    DelegatedAgentRequest,
    DelegatedAgentResult,
)
from lychd.domain.delegation.models import (
    DelegatedAgentJobStatus as Status,
)
from lychd.domain.delegation.services import IllegalDelegatedAgentTransitionError, UnknownDelegatedAgentJobError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class _Rows:
    def __init__(self, rows: list[DelegatedAgentEventRecord]) -> None:
        self.rows = rows

    def all(self) -> list[DelegatedAgentEventRecord]:
        return list(self.rows)


class _Session:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.locked = False
        self.snapshot: tuple[str, dict[str, Any] | None, list[DelegatedAgentEventRecord]] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        assert not self.locked

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[None]:
        try:
            yield
        except BaseException:
            if self.snapshot is not None:
                row = self.database.row
                assert row is not None
                row.status, row.result, self.database.events = self.snapshot
            raise
        finally:
            if self.locked:
                self.locked = False
                self.database.lock.release()

    async def scalar(self, statement: Any) -> Any:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM delegated_agent_job" in sql:
            assert "FOR UPDATE" in sql
            assert "delegated_agent_job.job_id =" in sql
            self.database.lock_queries += 1
            await self.database.lock.acquire()
            self.locked = True
            row = self.database.row
            if row is not None:
                self.snapshot = (row.status, deepcopy(row.result), list(self.database.events))
            return row
        assert self.locked
        assert "max(delegated_agent_event.seq)" in sql
        return len(self.database.events)

    async def scalars(self, statement: Any) -> _Rows:
        assert self.locked
        assert "delegated_agent_event" in str(statement)
        return _Rows(self.database.events)

    def add(self, event: DelegatedAgentEventRecord) -> None:
        assert self.locked
        self.database.events.append(event)

    async def flush(self) -> None:
        if self.database.flush_error is not None:
            raise self.database.flush_error


class _Database:
    """Model row locking and rollback only; execute the actual store methods/SQL."""

    def __init__(self, status: Status = Status.PREPARING) -> None:
        self.lock = asyncio.Lock()
        self.lock_queries = 0
        self.flush_error: RuntimeError | None = None
        req = DelegatedAgentRequest(run_id=str(uuid4()), step_id="step", runtime="reference", prompt="inert")
        result = None
        if status in TERMINAL_DELEGATED_AGENT_STATUSES:
            result = DelegatedAgentResult.model_validate(
                {
                    "job_id": "db-job",
                    "status": status,
                    "error": "existing winner" if status in {Status.FAILED, Status.TIMED_OUT, Status.LOST} else None,
                }
            ).model_dump(mode="json")
        self.row: DelegatedAgentJobRecord | None = DelegatedAgentJobRecord(
            id=uuid4(),
            job_id="db-job",
            request_id=req.request_id,
            run_id=UUID(req.run_id),
            runtime=req.runtime,
            profile=req.profile.value,
            request=req.model_dump(mode="json"),
            status=status.value,
            result=result,
        )
        states = [Status.QUEUED]
        if status is not Status.QUEUED:
            states.append(Status.ADMITTED)
        if status not in {Status.QUEUED, Status.ADMITTED}:
            states.append(Status.PREPARING)
        if status is Status.RUNNING or status in TERMINAL_DELEGATED_AGENT_STATUSES:
            states.append(Status.RUNNING)
        if status in TERMINAL_DELEGATED_AGENT_STATUSES:
            states.append(status)
        self.events: list[DelegatedAgentEventRecord] = []
        for seq, state in enumerate(states):
            event = DelegatedAgentEvent(
                job_id=self.row.job_id,
                request_id=req.request_id,
                seq=seq,
                kind=(
                    DelegatedAgentEventKind.RESULT_ADOPTED
                    if state in TERMINAL_DELEGATED_AGENT_STATUSES
                    else DelegatedAgentEventKind.STATUS_CHANGED
                ),
                status=state,
            )
            self.events.append(
                DelegatedAgentEventRecord(
                    job_record_id=self.row.id,
                    event_id=event.event_id,
                    seq=seq,
                    kind=event.kind.value,
                    status=state.value,
                    payload=event.model_dump(mode="json"),
                )
            )

    def __call__(self) -> _Session:
        return _Session(self)

    def store(self) -> DbDelegatedAgentJobStore:
        return DbDelegatedAgentJobStore(cast("async_sessionmaker[AsyncSession]", self))


def _lost() -> DelegatedAgentResult:
    return DelegatedAgentResult(job_id="db-job", status=Status.LOST, error="acceptance uncertain")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [Status.PREPARING, Status.RUNNING, *sorted(TERMINAL_DELEGATED_AGENT_STATUSES)])
async def test_db_conditional_settlement_preserves_winners_and_records_one_lost(state: Status) -> None:
    database = _Database(state)
    row = database.row
    assert row is not None
    original_result = deepcopy(row.result)
    original_events = [e.payload for e in database.events]
    store = database.store()
    job, changed = await store.settle_acceptance_failure("db-job", _lost())
    assert database.lock_queries == 1
    assert not database.lock.locked()
    assert changed is (state is Status.PREPARING)
    if changed:
        assert job.status is Status.LOST
        assert job.result == _lost()
        assert len(job.events) == len(original_events) + 1
        assert job.events[-1].kind is DelegatedAgentEventKind.RESULT_ADOPTED
        assert job.events[-1].seq == len(original_events)
    else:
        assert job.status is state
        assert row.result == original_result
        assert [e.payload for e in database.events] == original_events
    duplicate, changed = await store.settle_acceptance_failure("db-job", _lost())
    assert not changed
    assert duplicate == job


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["running", "lost"])
async def test_db_real_conditional_and_transition_methods_respect_lock_order(first: str) -> None:
    database = _Database()
    await database.lock.acquire()
    store = database.store()
    operations = [store.transition("db-job", Status.RUNNING), store.settle_acceptance_failure("db-job", _lost())]
    if first == "lost":
        operations.reverse()
    tasks = [asyncio.create_task(operation) for operation in operations]
    await asyncio.sleep(0)
    assert database.lock_queries == 2
    assert not any(task.done() for task in tasks)
    database.lock.release()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    assert database.row is not None
    if first == "running":
        assert not isinstance(results[1], BaseException)
        assert results[1][1] is False
        assert database.row.status == Status.RUNNING.value
    else:
        assert isinstance(results[1], IllegalDelegatedAgentTransitionError)
        assert database.row.status == Status.LOST.value
    assert len(database.events) == 4


@pytest.mark.asyncio
async def test_db_failed_conditional_event_write_rolls_back_row_and_event() -> None:
    database = _Database()
    database.flush_error = RuntimeError("event write unavailable")
    with pytest.raises(RuntimeError) as caught:
        await database.store().settle_acceptance_failure("db-job", _lost())
    assert caught.value is database.flush_error
    assert database.row is not None
    assert database.row.status == Status.PREPARING.value
    assert database.row.result is None
    assert len(database.events) == 3
    assert not database.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, Status.QUEUED, Status.ADMITTED])
async def test_db_conditional_settlement_rejects_missing_or_unexpected_rows(state: Status | None) -> None:
    database = _Database(state or Status.PREPARING)
    if state is None:
        database.row = None
    before = [e.payload for e in database.events]
    expected = UnknownDelegatedAgentJobError if state is None else IllegalDelegatedAgentTransitionError
    with pytest.raises(expected):
        await database.store().settle_acceptance_failure("db-job", _lost())
    assert [e.payload for e in database.events] == before
    if database.row is not None:
        assert state is not None
        assert database.row.status == state.value


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["identity", "status", "shape"])
@pytest.mark.parametrize("state", [Status.PREPARING, Status.RUNNING, Status.SUCCEEDED])
async def test_db_conditional_validates_result_even_when_preserving_a_winner(invalid: str, state: Status) -> None:
    database = _Database(state)
    assert database.row is not None
    before = (database.row.status, deepcopy(database.row.result), [e.payload for e in database.events])
    result = (
        DelegatedAgentResult(job_id="other-job", status=Status.LOST, error="wrong identity")
        if invalid == "identity"
        else DelegatedAgentResult(job_id="db-job", status=Status.SUCCEEDED)
        if invalid == "status"
        else DelegatedAgentResult.model_construct(job_id="db-job", status=Status.LOST, error=None)
    )
    with pytest.raises(ValueError, match=r"Result for job|LOST|error"):
        await database.store().settle_acceptance_failure("db-job", result)
    assert (database.row.status, database.row.result, [e.payload for e in database.events]) == before
