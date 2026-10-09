"""Bounded Run discovery reads metadata without hydrating private Run payloads."""

# Poison stored manifests to make accidental catalogue hydration fail.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from lychd.agents.router import Intent
from lychd.domain.cortex.ledger import DbRunLedger
from lychd.domain.cortex.runs import RunStatus

if TYPE_CHECKING:
    from litestar import Litestar
    from litestar.testing import TestClient


class _UnreadableManifest:
    def __deepcopy__(self, _memo: object) -> None:
        message = "Run discovery must not hydrate manifests or other private payloads."
        raise AssertionError(message)


def test_orb_catalogue_pages_every_status_with_timestamp_ties_and_no_payload_hydration(
    altar_client: TestClient[Litestar],
    fake_services: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instant = datetime(2026, 9, 8, 12, 30, 0, 123456, tzinfo=UTC)

    async def seed() -> None:
        for index in range(53):
            run_id = f"catalogue-{index:02d}"
            await fake_services.ledger.create(
                Intent(
                    session_id=f"session-{index}", run_id=run_id, prompt="private catalogue prompt", source="bridge"
                ),
                workflow_name="bridge_chat",
                queue_name="runs",
                priority=70,
            )
            record = fake_services.ledger._runs[run_id]
            record.created_at = instant
            record.pattern_manifest["private"] = _UnreadableManifest()
            if index % 2 == 0:
                await fake_services.ledger.set_status(run_id, RunStatus.FAILED, error="private failure detail")

    asyncio.run(seed())
    forbidden = AsyncMock(side_effect=AssertionError("Catalogue hydrated a selected Run or event history."))
    monkeypatch.setattr(fake_services.ledger, "get", forbidden)
    monkeypatch.setattr(fake_services.ledger, "list_events", forbidden)
    first = altar_client.get("/api/v1/orb/runs?limit=50")
    assert first.status_code == 200
    assert first.json()["capture"] == "process_local"
    assert len(first.json()["runs"]) == 50
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    second = altar_client.get("/api/v1/orb/runs", params={"limit": 50, "cursor": cursor})
    assert second.status_code == 200
    combined = first.json()["runs"] + second.json()["runs"]
    assert [run["run_id"] for run in combined] == [f"catalogue-{index:02d}" for index in reversed(range(53))]
    assert {run["status"] for run in combined} == {"queued", "failed"}
    assert all(run["created_at"] == "2026-09-08T12:30:00.123456Z" for run in combined)
    assert second.json()["next_cursor"] is None
    assert "private catalogue prompt" not in first.text + second.text
    assert "private failure detail" not in first.text + second.text
    forbidden.assert_not_awaited()


@pytest.mark.parametrize(
    "cursor",
    [
        "%",
        "x" * 513,
        base64.urlsafe_b64encode(b"{}").decode(),
        base64.urlsafe_b64encode(b'{"created_at":"2026-09-08","run_id":"run"}').decode(),
        base64.urlsafe_b64encode(b'{"created_at":"2026-09-08T00:00:00Z","run_id":"../escape"}').decode(),
    ],
)
def test_orb_catalogue_cursor_is_bounded_and_validated(altar_client: TestClient[Litestar], cursor: str) -> None:
    assert altar_client.get("/api/v1/orb/runs", params={"cursor": cursor}).status_code == 400


def test_orb_catalogue_requires_read_access_and_bounds_requested_page(altar_client: TestClient[Litestar]) -> None:
    assert altar_client.get("/api/v1/orb/runs", headers={"authorization": ""}).status_code == 401
    assert altar_client.get("/api/v1/orb/runs?limit=51").status_code == 400
    assert altar_client.get("/api/v1/orb/runs?limit=0").status_code == 400
    assert altar_client.get("/api/v1/orb/runs").json()["runs"] == []


@pytest.mark.asyncio
async def test_db_orb_catalogue_selects_safe_metadata_and_limits_before_hydration() -> None:
    statements: list[Any] = []

    async def execute(statement: Any) -> Any:
        statements.append(statement)
        return SimpleNamespace(all=lambda: cast("list[Any]", []))

    session = AsyncMock()
    session.execute.side_effect = execute
    session.__aenter__.return_value = session
    ledger = DbRunLedger(session_factory=cast("Any", lambda: session))
    await ledger.list_run_summaries(
        before=(datetime(2026, 9, 8, tzinfo=UTC), "00000000-0000-0000-0000-000000000001"), limit=999
    )

    assert len(statements) == 1
    statement = statements[0]
    assert [column.name for column in statement.selected_columns] == [
        "id",
        "session_id",
        "workflow_name",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "error_present",
    ]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 51" in sql
    assert "run.created_at <" in sql
    assert "run.id <" in sql
    assert "run.created_at DESC, run.id DESC" in sql
    assert "run.error IS NOT NULL AS error_present" in sql
    assert "prompt" not in sql
    assert "pattern_manifest" not in sql
