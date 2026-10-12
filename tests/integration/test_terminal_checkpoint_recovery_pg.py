"""Real PostgreSQL cleanup failure and recovery through replacement startup adapters.

The startup helper and Run/Step/checkpoint stores are real. This does not start an
application process, broker or model, and the reused fixture creates ORM tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest

pytest.importorskip("testcontainers", reason="optional disposable PostgreSQL receipt")

from sqlalchemy import select, text

from lychd.agents.router import Intent
from lychd.db.checkpoints import PostgresStasisStore
from lychd.db.models import Step
from lychd.domain.cortex.engine import QueueRouter, RouteRule, RunEngine
from lychd.domain.cortex.events import InProcessEventBus, RunEvent, RunEventKind
from lychd.domain.cortex.ledger import DbRunLedger
from lychd.domain.cortex.runs import RunStatus

# Exercise the actual startup helper without launching an application or broker.
from lychd.interface.web.lifespan import (
    _reconcile_terminal_checkpoints_at_startup,  # pyright: ignore[reportPrivateUsage]
)
from tests.integration import test_db_run_ledger_pg as database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.container]
pg_url = database.pg_url
pg_factory = database.pg_factory


def _engine(factory: async_sessionmaker[AsyncSession]) -> RunEngine:
    ledger = DbRunLedger(session_factory=factory)
    return RunEngine(
        ledger=ledger,
        bus=InProcessEventBus(ledger=ledger),
        workflows=None,
        queue_router=QueueRouter(routing={"default": RouteRule(queue="runs", priority=50)}),
        queues={},
        stasis_store=PostgresStasisStore(factory),
    )


async def test_failed_checkpoint_delete_preserves_later_evidence_and_recovers(
    pg_factory: async_sessionmaker[AsyncSession],
) -> None:
    engine = _engine(pg_factory)
    run_ids: list[str] = []
    for index in range(2):
        run = await engine.ledger.create(
            Intent(session_id="s", prompt="public recovery fixture", source="bridge"),
            workflow_name="bridge_chat",
            queue_name="runs",
            priority=50,
        )
        run_ids.append(run.run_id)
        await engine.ledger.set_status(run.run_id, RunStatus.RUNNING)
        await engine.ledger.append_event(RunEvent(run_id=run.run_id, seq=0, kind=RunEventKind.STATUS, data="running"))
        await engine.ledger.set_status(run.run_id, RunStatus.DONE)
        await engine.stasis_store.replace(run.run_id, [{"reject_delete": index == 0}])
    async with pg_factory() as session, session.begin():
        await session.execute(
            text("""
            CREATE FUNCTION reject_fixture_checkpoint_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.snapshots -> 0 ->> 'reject_delete' = 'true' THEN
                    RAISE EXCEPTION 'fixture checkpoint deletion refused';
                END IF;
                RETURN OLD;
            END $$
        """)
        )
        await session.execute(
            text("""
            CREATE TRIGGER reject_fixture_checkpoint_delete BEFORE DELETE ON run_checkpoint
            FOR EACH ROW EXECUTE FUNCTION reject_fixture_checkpoint_delete()
        """)
        )
    try:
        await _reconcile_terminal_checkpoints_at_startup(engine, required=True)
        assert await engine.stasis_store.exists(run_ids[0])
        assert not await engine.stasis_store.exists(run_ids[1])
        for run_id in run_ids:
            terminal = await engine.ledger.latest_event(run_id, RunEventKind.DONE)
            assert terminal is not None
            assert terminal.data == "done"
            assert terminal.seq == 1
    finally:
        async with pg_factory() as session, session.begin():
            await session.execute(text("DROP FUNCTION reject_fixture_checkpoint_delete() CASCADE"))

    # Replace every in-process owner, then repeat the healthy sweep: neither
    # retained debt nor an already repaired terminal event authorizes replay.
    restarted = _engine(pg_factory)
    await _reconcile_terminal_checkpoints_at_startup(restarted, required=True)
    await _reconcile_terminal_checkpoints_at_startup(restarted, required=True)
    for run_id in run_ids:
        assert not await restarted.stasis_store.exists(run_id)
        row = await restarted.ledger.get(run_id)
        assert row is not None
        assert row.status is RunStatus.DONE
        async with pg_factory() as session:
            sequences = await session.scalars(select(Step.seq).where(Step.run_id == UUID(run_id)).order_by(Step.seq))
            assert list(sequences) == [0, 1]
