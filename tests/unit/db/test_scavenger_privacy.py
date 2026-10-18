"""Private database values never become generic transaction failure evidence."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import StatementError

from lychd.compositions.scavenger.schemas import LiteralFilter, ScavengerRefused
from lychd.config.logging import apply_logging, build_log_config
from lychd.config.settings import Settings
from lychd.config.settings.server import DatabaseSettings
from lychd.db.factory import create_db_engine
from lychd.db.scavenger import DbScavengerStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def factory_with_failure(stage: str, error: BaseException) -> Any:
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    if stage == "body":
        session.execute.side_effect = error
    elif stage == "commit":
        session.begin.return_value.__aexit__.side_effect = error
    else:
        factory.return_value.__aexit__.side_effect = error
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["body", "commit", "close"])
@pytest.mark.parametrize("json_output", [False, True])
async def test_transaction_failures_hide_driver_messages_parameters_and_chained_private_values(
    stage: str, *, json_output: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "synthetic-private-seed-and-item-value"
    error = StatementError(
        "storage failed",
        "INSERT INTO scavenger_observation VALUES ($1)",
        (marker.encode(),),
        ValueError(marker),
    )
    factory = factory_with_failure(stage, error)
    store = DbScavengerStore(cast("async_sessionmaker[AsyncSession]", factory), pattern={})
    apply_logging(force_json=json_output)
    structlog.configure(cache_logger_on_first_use=False)
    try:

        async def invoke_and_log() -> None:
            try:
                await store.active(owner="magus", authorize=lambda: None)
            except ScavengerRefused:
                structlog.get_logger("scavenger.privacy-test").exception("Application storage refused")
                raise

        with pytest.raises(ScavengerRefused, match=r"^application_storage$") as caught:
            await invoke_and_log()
        assert caught.value.__suppress_context__ is True
        captured = capsys.readouterr()
        assert "Application storage refused" in captured.err
        assert marker not in captured.err + captured.out
        assert "INSERT INTO" not in captured.err + captured.out
    finally:
        structlog.reset_defaults()


@pytest.mark.asyncio
async def test_database_engine_hides_actual_sqlalchemy_parameter_rendering_even_with_echo() -> None:
    engine = create_db_engine(DatabaseSettings(password=SecretStr("synthetic-only"), echo=True))
    try:
        error = StatementError(
            "query failed",
            "INSERT INTO private_values VALUES ($1)",
            ("synthetic-private-parameter",),
            ValueError("driver unavailable"),
            hide_parameters=engine.sync_engine.hide_parameters,
        )
        assert "synthetic-private-parameter" not in str(error)
        assert "parameters hidden" in str(error)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("json_output", [False, True])
def test_sql_diagnostics_never_render_private_result_rows(
    *, json_output: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings()
    settings.server.logging.sqlalchemy_level = 10
    config = build_log_config(render_as_json=json_output, settings=settings)
    stdlib = config.structlog_logging_config.standard_lib_logging_config
    assert stdlib is not None
    stdlib.configure()
    marker = "synthetic-private-returned-row"
    engine = create_engine("sqlite://", hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT :private_value"), {"private_value": marker}).scalar_one() == marker
        captured = capsys.readouterr()
        assert "SELECT" in captured.err
        assert "parameters hidden" in captured.err
        assert marker not in captured.err + captured.out
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_current_policy_refusal_and_cancellation_are_not_reclassified_as_storage_failure() -> None:
    for error in (asyncio.CancelledError(), ScavengerRefused("application_authority")):
        factory = factory_with_failure("body", error)
        store = DbScavengerStore(cast("async_sessionmaker[AsyncSession]", factory), pattern={})
        with pytest.raises(type(error)) as caught:
            await store.active(owner="magus", authorize=lambda: None)
        assert caught.value is error


@pytest.mark.asyncio
async def test_corrupt_retained_value_is_a_closed_custody_failure() -> None:
    with pytest.raises(ValidationError) as invalid:
        LiteralFilter(query="synthetic-private-filter" * 100)
    factory = factory_with_failure("body", invalid.value)
    store = DbScavengerStore(cast("async_sessionmaker[AsyncSession]", factory), pattern={})
    with pytest.raises(ScavengerRefused, match=r"^application_custody$") as caught:
        await store.active(owner="magus", authorize=lambda: None)
    assert caught.value.__suppress_context__ is True
