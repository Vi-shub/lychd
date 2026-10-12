"""Derivations use the actual bootstrapped restricted runtime role."""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from lychd.config.settings import Settings
from lychd.db.factory import create_db_engine, database_saq_dsn
from tests.integration import test_database_authority_pg as authority
from tests.integration.test_archive_derivation_pg import setup
from tests.integration.test_archive_pg import SUBJECT

pytestmark = [pytest.mark.integration, pytest.mark.container]
database = authority.database


def test_runtime_role_derivation_and_retirement(database: Settings) -> None:
    async def exercise() -> None:
        engine = create_db_engine(database.server.database, runtime=True)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            _, _, _, derivation, request = await setup(factory)
            result = await derivation.derive(SUBJECT, request, purpose="derive")
            assert result.state == "complete"
            await derivation.retire(SUBJECT, derivation.reference, expected_version=1, purpose="manage")
            assert (await derivation.inspect(SUBJECT, request, purpose="status")).state == "invalidated"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    dsn = database_saq_dsn(database.server.database, runtime=True)
    for statement in (
        "ALTER TABLE archive_note_embedding ADD COLUMN forbidden integer",
        "CREATE EXTENSION hstore",
        "UPDATE lychd_db_version SET version_num='bad'",
    ):
        with psycopg.connect(dsn, autocommit=True) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)
