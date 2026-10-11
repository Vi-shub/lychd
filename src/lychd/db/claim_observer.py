"""Disposable, deadline-bound PostgreSQL observation after a failed Run claim.

The original mutator must already have completed. This helper neither bounds nor
fences a live application task. asyncpg owns startup/CancelRequest transports and
possible DNS work; termination is not proof of completed server rollback.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import asyncpg

from lychd.db.authority import RUNTIME_AUTHORITY_QUERY, require_runtime_authority
from lychd.domain.cortex.ledger import RunClaimObservation, RunClaimState
from lychd.domain.cortex.runs import RunDeliveryRecord, RunDeliveryState, RunStatus

if TYPE_CHECKING:
    from lychd.config.settings.server import DatabaseSettings

CLAIM_OBSERVATION_TIMEOUT_S = 5.0


async def observe_claim(run_id: str, *, enqueue_seq: int, settings: DatabaseSettings) -> RunClaimObservation:
    """Lock Run then exact delivery once, admitting evidence only before cutoff."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLAIM_OBSERVATION_TIMEOUT_S
    connection: Any = None

    def remaining() -> float:
        budget = deadline - loop.time()
        if budget <= 0:
            raise TimeoutError
        return budget

    def server_budget() -> str:
        # PostgreSQL interprets zero as disabling the timeout.
        return str(max(1, int(remaining() * 1000)))

    async with asyncio.timeout_at(deadline):
        try:
            password = settings.runtime_password
            if password is None:
                msg = "Required runtime database password is unavailable in Settings."
                raise ValueError(msg)
            connection = await cast("Any", asyncpg).connect(
                host=settings.host,
                port=settings.port,
                database=settings.database,
                user=settings.runtime_user,
                password=password.get_secret_value(),
                target_session_attrs="any",
                timeout=remaining(),
                command_timeout=remaining(),
                server_settings={"statement_timeout": server_budget(), "lock_timeout": server_budget()},
            )
            require_runtime_authority(await connection.fetchval(RUNTIME_AUTHORITY_QUERY, timeout=remaining()))
            await connection.execute("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE", timeout=remaining())
            await connection.execute(
                "SELECT set_config('statement_timeout', $1, true), set_config('lock_timeout', $1, true)",
                server_budget(),
                timeout=remaining(),
            )
            run = await connection.fetchrow(
                'SELECT id, enqueue_seq, status, queue_name, priority FROM "run" WHERE id = $1 FOR UPDATE',
                UUID(run_id),
                timeout=remaining(),
            )
            await connection.execute(
                "SELECT set_config('statement_timeout', $1, true), set_config('lock_timeout', $1, true)",
                server_budget(),
                timeout=remaining(),
            )
            delivery = await connection.fetchrow(
                "SELECT run_id, enqueue_seq, state, queue_name, priority, resume "
                "FROM run_delivery WHERE run_id = $1 AND enqueue_seq = $2 FOR UPDATE",
                UUID(run_id),
                enqueue_seq,
                timeout=remaining(),
            )
            observation = RunClaimObservation(
                RunClaimState(
                    str(run["id"]), run["enqueue_seq"], RunStatus(run["status"]), run["queue_name"], run["priority"]
                )
                if run is not None
                else None,
                RunDeliveryRecord(
                    run_id=str(delivery["run_id"]),
                    enqueue_seq=delivery["enqueue_seq"],
                    state=RunDeliveryState(delivery["state"]),
                    queue_name=delivery["queue_name"],
                    priority=delivery["priority"],
                    resume=delivery["resume"],
                )
                if delivery is not None
                else None,
            )
            await connection.execute("ROLLBACK", timeout=remaining())
        finally:
            # No graceful close or awaited rollback on failure/timeout unwind.
            if connection is not None:
                connection.terminate()
        remaining()
    # Also reject a late synchronous projection/termination that outran the timer.
    remaining()
    return observation
