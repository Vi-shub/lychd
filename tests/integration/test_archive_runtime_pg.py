"""Archive usability through the actual bootstrap's restricted runtime role."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from lychd.config.settings.root import Settings
from lychd.db.archive import DbArchiveStore
from lychd.db.factory import create_db_engine, database_saq_dsn
from lychd.domain.archive import (
    ArchiveAuthorization,
    ArchiveProfile,
    ArchiveService,
    ArchiveSubject,
    NoteProposal,
    RecallRequest,
    text_digest,
)
from tests.integration import test_database_authority_pg as authority_profile

database = authority_profile.database

pytestmark = [pytest.mark.integration, pytest.mark.container]


class Authority:
    def __init__(self, profile: ArchiveProfile) -> None:
        self.profile = profile

    def authorize(self, request: ArchiveAuthorization) -> bool:
        return (
            request.subject == self.profile.owner_subject
            and request.namespace_id == self.profile.namespace_id
            and request.policy_revision == self.profile.policy_revision
        )


def test_bootstrapped_runtime_can_use_archive_without_ddl_authority(database: Settings) -> None:
    async def exercise() -> None:
        engine = create_db_engine(database.server.database, runtime=True)
        subject = ArchiveSubject(identity="trusted-runtime-fixture")
        profile = ArchiveProfile(namespace_id=uuid4(), owner_subject=subject, policy_revision="public-notes.1")
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            archive = ArchiveService(profile=profile, store=DbArchiveStore(factory), authorizer=Authority(profile))
            note = NoteProposal(
                namespace_id=profile.namespace_id,
                request_id=uuid4(),
                source_key="runtime-note",
                source_revision="1",
                source_digest=text_digest("The red fox is public."),
                text="The red fox is public.",
                producer="operator",
                observed_at=datetime.now(UTC),
            )
            admitted = await archive.admit(subject, note, purpose="retain-public-note")
            await archive.promote(
                subject, admitted.reference.record_id, expected_version=1, purpose="retain-public-note"
            )
            request = RecallRequest(namespace_id=profile.namespace_id, request_id=uuid4(), query="fox")
            receipt = await archive.recall(subject, request, purpose="answer")
            restarted = ArchiveService(profile=profile, store=DbArchiveStore(factory), authorizer=Authority(profile))
            projection = await restarted.resolve_receipt(
                subject, receipt.receipt_id, receipt.receipt_digest, purpose="answer"
            )
            assert tuple(item.text for item in projection.items) == (note.text,)
            await restarted.revoke(
                subject, admitted.reference.record_id, expected_version=2, purpose="retain-public-note"
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    dsn = database_saq_dsn(database.server.database, runtime=True)
    for statement in (
        "CREATE TABLE public.archive_forbidden(id integer)",
        "ALTER TABLE public.archive_note ADD COLUMN forbidden integer",
        "UPDATE lychd_db_version SET version_num='unauthorized'",
    ):
        with psycopg.connect(dsn, autocommit=True) as connection, pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(statement)
