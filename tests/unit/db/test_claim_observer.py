"""Native observer lifecycle tests with no sockets or database services."""

# ruff: noqa: ASYNC109 - native-driver fakes retain the actual timeout keyword contract

from __future__ import annotations

import asyncio
import time
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from lychd.config.settings.server import DatabaseSettings
from lychd.db import claim_observer
from lychd.db.authority import RUNTIME_AUTHORITY_QUERY, DatabaseAuthorityError
from lychd.domain.cortex.runs import RunDeliveryState, RunStatus

RUN_ID = "15e62b81-66e6-4ed8-a7ec-027465ff33b7"


class NativeConnection:
    def __init__(self, *, authority: object = False, fail: str | None = None) -> None:
        self.authority = authority
        self.fail = fail
        self.calls: list[tuple[str, tuple[Any, ...], float | None]] = []
        self.terminated = False
        self.termination_delay = 0.0
        self.phase_delay = 0.0
        self.cancel_at: str | None = None
        self.entered = asyncio.Event()
        self.run: dict[str, Any] | None = {
            "id": UUID(RUN_ID),
            "enqueue_seq": 3,
            "status": "running",
            "queue_name": "runs",
            "priority": 50,
        }
        self.delivery: dict[str, Any] | None = {
            "run_id": UUID(RUN_ID),
            "enqueue_seq": 3,
            "state": "claimed",
            "queue_name": "runs",
            "priority": 50,
            "resume": False,
        }

    async def step(self, phase: str, *args: Any, timeout: float | None = None) -> None:
        assert not self.terminated
        self.calls.append((phase, args, timeout))
        if self.phase_delay:
            await asyncio.sleep(self.phase_delay)
        if phase == self.cancel_at:
            self.entered.set()
            await asyncio.Event().wait()
        if phase == self.fail:
            raise RuntimeError(phase)

    async def fetchval(self, query: str, *, timeout: float) -> object:
        assert query == RUNTIME_AUTHORITY_QUERY
        await self.step("authority", query, timeout=timeout)
        return self.authority

    async def execute(self, query: str, *args: Any, timeout: float) -> str:
        await self.step(query, *args, timeout=timeout)
        return "OK"

    async def fetchrow(self, query: str, *args: Any, timeout: float) -> dict[str, Any] | None:
        assert "FOR UPDATE" in query
        assert "SKIP LOCKED" not in query
        assert "NOWAIT" not in query
        if 'FROM "run"' in query:
            assert args == (UUID(RUN_ID),)
            await self.step("run", query, *args, timeout=timeout)
            return self.run
        assert "FROM run_delivery" in query
        assert args == (UUID(RUN_ID), 3)
        await self.step("delivery", query, *args, timeout=timeout)
        return self.delivery

    def terminate(self) -> None:
        self.calls.append(("terminate", (), None))
        self.terminated = True
        if self.termination_delay:
            time.sleep(self.termination_delay)

    async def close(self) -> None:
        pytest.fail("The disposable observer must never await graceful close")


@pytest.fixture
def settings() -> DatabaseSettings:
    return DatabaseSettings(
        host="runtime.example",
        port=6543,
        database="application",
        user="bootstrap_admin",
        password=SecretStr("admin-never-used"),
        runtime_user="runtime_reader",
        runtime_password=SecretStr("loaded:p@ss/word"),
    )


def install(monkeypatch: pytest.MonkeyPatch, connection: NativeConnection) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def connect(**kwargs: Any) -> NativeConnection:
        calls.append(kwargs)
        return connection

    monkeypatch.setattr(claim_observer.asyncpg, "connect", connect)
    return calls


@pytest.mark.asyncio
async def test_exact_runtime_credentials_authority_lock_order_and_termination(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
) -> None:
    connection = NativeConnection()
    connects = install(monkeypatch, connection)
    tasks = asyncio.all_tasks()
    result = await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert asyncio.all_tasks() == tasks
    assert len(connects) == 1
    kwargs = connects[0]
    assert {key: kwargs[key] for key in ("host", "port", "database", "user", "password")} == {
        "host": "runtime.example",
        "port": 6543,
        "database": "application",
        "user": "runtime_reader",
        "password": "loaded:p@ss/word",
    }
    assert kwargs["target_session_attrs"] == "any"
    assert "ssl" not in kwargs  # Preserve the shared factory's native security defaults.
    assert 0 < kwargs["timeout"] <= 5
    assert 0 < kwargs["command_timeout"] <= 5
    assert all(0 < int(value) <= 5000 for value in kwargs["server_settings"].values())
    phases = [call[0] for call in connection.calls]
    assert phases[0] == "authority"
    assert phases[1] == "BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE"
    assert phases.index("run") < phases.index("delivery")
    assert phases[-2:] == ["ROLLBACK", "terminate"]
    assert not any(phase.startswith(("UPDATE", "INSERT", "DELETE", "COMMIT")) for phase in phases)
    timeouts = [value for _, _, value in connection.calls if value is not None]
    assert timeouts == sorted(timeouts, reverse=True)
    assert all(0 < value <= 5 for value in timeouts)
    for phase, args, _ in connection.calls:
        if "set_config" in phase:
            assert 0 < int(args[0]) <= 5000
    assert result.run is not None
    assert result.run.status is RunStatus.RUNNING
    assert result.delivery is not None
    assert result.delivery.state is RunDeliveryState.CLAIMED
    assert connection.terminated
    with pytest.raises(FrozenInstanceError):
        result.run.status = RunStatus.QUEUED  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", [None, True, 0, "false"])
async def test_authority_refusal_precedes_application_reads(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
    authority: object,
) -> None:
    connection = NativeConnection(authority=authority)
    install(monkeypatch, connection)
    with pytest.raises(DatabaseAuthorityError):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert [call[0] for call in connection.calls] == ["authority", "terminate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["authority", "BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE", "run", "delivery", "ROLLBACK"]
)
async def test_failure_terminates_without_rollback_or_close_unwind(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
    phase: str,
) -> None:
    connection = NativeConnection(fail=phase)
    install(monkeypatch, connection)
    with pytest.raises(RuntimeError, match=phase):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    phases = [call[0] for call in connection.calls]
    assert phases[-2:] == [phase, "terminate"]
    assert phases.count("ROLLBACK") == int(phase == "ROLLBACK")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["authority", "run", "delivery", "ROLLBACK"])
async def test_timeout_terminates_and_never_accepts_partial_evidence(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
    phase: str,
) -> None:
    connection = NativeConnection()
    connection.cancel_at = phase
    install(monkeypatch, connection)
    monkeypatch.setattr(claim_observer, "CLAIM_OBSERVATION_TIMEOUT_S", 0.02)
    tasks = asyncio.all_tasks()
    with pytest.raises(TimeoutError):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert asyncio.all_tasks() == tasks
    assert connection.terminated
    assert [call[0] for call in connection.calls][-2:] == [phase, "terminate"]


@pytest.mark.asyncio
async def test_one_budget_covers_all_phases(monkeypatch: pytest.MonkeyPatch, settings: DatabaseSettings) -> None:
    connection = NativeConnection()
    connection.phase_delay = 0.008
    install(monkeypatch, connection)
    monkeypatch.setattr(claim_observer, "CLAIM_OBSERVATION_TIMEOUT_S", 0.02)
    with pytest.raises(TimeoutError):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert connection.terminated
    assert "delivery" not in [call[0] for call in connection.calls]


@pytest.mark.asyncio
async def test_explicit_cutoff_includes_synchronous_termination(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
) -> None:
    connection = NativeConnection()
    connection.termination_delay = 0.03
    install(monkeypatch, connection)
    monkeypatch.setattr(claim_observer, "CLAIM_OBSERVATION_TIMEOUT_S", 0.02)
    with pytest.raises(TimeoutError):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert connection.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize("late_handle", [False, True])
async def test_connect_timeout_never_adopts_late_handle(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
    *,
    late_handle: bool,
) -> None:
    connection = NativeConnection()
    calls = 0

    async def connect(**_kwargs: Any) -> NativeConnection:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not late_handle:
                raise
        return connection

    monkeypatch.setattr(claim_observer.asyncpg, "connect", connect)
    monkeypatch.setattr(claim_observer, "CLAIM_OBSERVATION_TIMEOUT_S", 0.02)
    with pytest.raises(TimeoutError):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert calls == 1
    assert connection.terminated is late_handle
    assert all(call[0] == "terminate" for call in connection.calls)


@pytest.mark.asyncio
async def test_caller_cancellation_terminates_acquired_handle(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
) -> None:
    connection = NativeConnection()
    connection.cancel_at = "run"
    install(monkeypatch, connection)
    task = asyncio.create_task(claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings))
    await connection.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in connection.calls][-2:] == ["run", "terminate"]


@pytest.mark.asyncio
async def test_missing_loaded_password_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = NativeConnection()
    calls = install(monkeypatch, connection)
    with pytest.raises(ValueError, match="runtime database password"):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=DatabaseSettings())
    assert calls == []


@pytest.mark.asyncio
async def test_malformed_projection_terminates_without_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    settings: DatabaseSettings,
) -> None:
    connection = NativeConnection()
    assert connection.run is not None
    connection.run["status"] = "not-a-run-status"
    install(monkeypatch, connection)
    with pytest.raises(ValueError, match="not-a-run-status"):
        await claim_observer.observe_claim(RUN_ID, enqueue_seq=3, settings=settings)
    assert [call[0] for call in connection.calls][-2:] == ["delivery", "terminate"]
