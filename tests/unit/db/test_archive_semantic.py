"""Effect-free receipt/inspection and transaction failure boundaries."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from traceback import format_exception
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from pydantic import TypeAdapter
from sqlalchemy.exc import StatementError

from lychd.db.archive import DbArchiveStore
from lychd.db.archive_semantic import DbArchiveSemanticStore
from lychd.db.models.archive import ArchiveNamespace, ArchiveRecall
from lychd.db.models.archive_derivation import ArchiveEmbeddingGeneration
from lychd.domain.animation.embeddings import EmbeddingBinding, embedding_digest, embedding_vector_digest
from lychd.domain.archive.contracts import (
    ArchiveProfile,
    ArchiveRefused,
    ArchiveSubject,
    RecallRequest,
    digest,
    text_digest,
)
from lychd.domain.archive.derivation import GenerationManifest, GenerationRef
from lychd.domain.archive.semantic import (
    SemanticPrepared,
    SemanticQueryExecution,
    SemanticRecallReceipt,
    SemanticRecallRequest,
)
from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE

SUBJECT = ArchiveSubject(identity="semantic-owner")
ARCHIVE = ArchiveProfile(namespace_id=UUID(int=1), owner_subject=SUBJECT, policy_revision="one")
MANIFEST = GenerationManifest(
    archive=ARCHIVE,
    embedding=PROFILE,
    binding=EmbeddingBinding(capability_key="local:embedding:fixed", profile_digest=PROFILE.digest),
)
REFERENCE = GenerationRef(generation_id=UUID(int=2), manifest_digest=MANIFEST.digest)
REQUEST = SemanticRecallRequest(
    namespace_id=ARCHIVE.namespace_id,
    request_id=UUID(int=3),
    generation=REFERENCE,
    query="QUERY_MUST_NOT_ESCAPE_STORAGE_ERROR",
    min_cosine=-1.0,
)
VECTOR = (1.0, *(0.0 for _ in range(1023)))


def execution() -> SemanticQueryExecution:
    metadata = {
        "item_id": str(REQUEST.request_id),
        "input_digest": text_digest(REQUEST.query),
        "rendered_digest": text_digest(PROFILE.query_prefix + REQUEST.query),
        "vector_digest": embedding_vector_digest(VECTOR),
        "tokens": 3,
    }
    return SemanticQueryExecution.model_validate(
        {
            "execution_id": UUID(int=4),
            "grant_id": UUID(int=5),
            "profile_digest": PROFILE.digest,
            "receipt_digest": embedding_digest(
                {"profile": PROFILE.digest, "operation": "embed_query", "items": [metadata]}
            ),
            **metadata,
        }
    )


def retained() -> ArchiveRecall:
    values: dict[str, Any] = {
        "schema_version": 2,
        "receipt_id": UUID(int=6),
        "request_id": REQUEST.request_id,
        "namespace_id": ARCHIVE.namespace_id,
        "subject": SUBJECT,
        "purpose": "answer",
        "profile": ARCHIVE.profile,
        "retrieval_policy": "postgres-exact-cosine@1",
        "policy_revision": ARCHIVE.policy_revision,
        "query_digest": text_digest(REQUEST.query),
        "request_digest": REQUEST.binding_digest(MANIFEST, SUBJECT, "answer"),
        "run_id": None,
        "max_results": REQUEST.max_results,
        "char_budget": REQUEST.char_budget,
        "min_cosine": -1.0,
        "generation": REFERENCE,
        "generation_version": 1,
        "query_execution": execution(),
        "coverage": {"promoted_notes": 0, "eligible_notes": 0},
        "selected": [],
        "omitted_for_budget": 0,
        "created_at": datetime(2026, 10, 19, tzinfo=UTC),
    }
    values = TypeAdapter(dict[str, Any]).dump_python(values, mode="json")
    values["receipt_digest"] = digest(values)
    receipt = SemanticRecallReceipt.model_validate(values)
    return ArchiveRecall(
        id=receipt.receipt_id,
        namespace_id=receipt.namespace_id,
        request_id=receipt.request_id,
        request_digest=receipt.request_digest,
        receipt_digest=receipt.receipt_digest,
        receipt=receipt.model_dump(mode="json"),
    )


def namespace() -> ArchiveNamespace:
    return ArchiveNamespace(
        id=ARCHIVE.namespace_id, owner_subject=SUBJECT.identity, policy_revision=ARCHIVE.policy_revision
    )


def check(object_id: UUID | None, binding_digest: str) -> None:
    pass


def session_mock() -> AsyncMock:
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.begin = Mock(return_value=AsyncMock())
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing_namespace", "missing_receipt", "retained"])
async def test_inspection_only_reads_retained_metadata_and_never_initializes(state: str) -> None:
    session = session_mock()
    row = retained()
    session.scalar.side_effect = (
        [None] if state == "missing_namespace" else [namespace(), row if state == "retained" else None]
    )
    store = DbArchiveSemanticStore(cast("Any", lambda: session), generation_id=REFERENCE.generation_id)
    actual = await store.inspect(MANIFEST, REQUEST, check)
    assert actual == (SemanticRecallReceipt.model_validate(row.receipt) if state == "retained" else None)
    for call in session.scalar.await_args_list:
        statement = call.args[0]
        assert {table.name for table in statement.get_final_froms()} <= {"archive_namespace", "archive_recall"}
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("field", ["id", "namespace_id", "request_id", "request_digest", "receipt_digest", "receipt"])
@pytest.mark.asyncio
async def test_receipt_parser_closes_physical_row_identity_and_digest(field: str) -> None:
    row = retained()
    setattr(row, field, {} if field == "receipt" else ("0" * 64 if "digest" in field else UUID(int=99)))
    session = session_mock()
    session.scalar.side_effect = [namespace(), row]
    with pytest.raises(ArchiveRefused, match=r"receipt_integrity|receipt_schema"):
        await DbArchiveSemanticStore(cast("Any", lambda: session), generation_id=REFERENCE.generation_id).inspect(
            MANIFEST, REQUEST, check
        )


@pytest.mark.asyncio
async def test_lexical_reader_explicitly_refuses_semantic_schema() -> None:
    session = session_mock()
    session.scalar.side_effect = [namespace(), retained()]
    with pytest.raises(ArchiveRefused, match="receipt_schema"):
        await DbArchiveStore(cast("Any", lambda: session)).inspect_recall(
            ARCHIVE,
            RecallRequest(namespace_id=ARCHIVE.namespace_id, request_id=REQUEST.request_id, query=REQUEST.query),
            check,
        )


@pytest.mark.asyncio
async def test_complete_seals_canonical_utc_receipt_then_fully_validates() -> None:
    session = session_mock()
    session.add = Mock()
    session.execute.return_value = SimpleNamespace(all=list)
    session.scalar.side_effect = [
        namespace(),
        None,
        ArchiveEmbeddingGeneration(
            id=REFERENCE.generation_id,
            namespace_id=ARCHIVE.namespace_id,
            manifest_digest=MANIFEST.digest,
            manifest=MANIFEST.model_dump(mode="json"),
            state="active",
            version=1,
        ),
        0,
        0,
    ]
    store = DbArchiveSemanticStore(cast("Any", lambda: session), generation_id=REFERENCE.generation_id)
    actual = await store.complete(
        MANIFEST, SUBJECT, REQUEST, "answer", SemanticPrepared(generation_version=1), execution(), VECTOR, check
    )
    assert actual.created_at.utcoffset() is not None
    assert actual.model_dump(mode="json")["created_at"].endswith("Z")
    assert actual.computed_digest() == actual.receipt_digest
    assert SemanticRecallReceipt.model_validate(actual.model_dump(mode="json")) == actual
    assert SemanticRecallReceipt.model_validate(session.add.call_args.args[0].receipt) == actual


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["begin", "commit", "close"])
async def test_storage_failures_outside_sql_body_remain_safe(phase: str) -> None:
    session = session_mock()
    session.scalar.side_effect = [namespace(), retained()]
    error = StatementError("failure", "SELECT :query", {"query": REQUEST.query}, RuntimeError(REQUEST.query))
    closed: list[bool] = []

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        if phase == "begin":
            raise error
        yield
        if phase == "commit":
            raise error

    @asynccontextmanager
    async def factory() -> AsyncGenerator[Any]:
        try:
            yield session
        finally:
            closed.append(True)
            if phase == "close":
                raise error

    session.begin = Mock(side_effect=transaction)
    store = DbArchiveSemanticStore(cast("Any", factory), generation_id=REFERENCE.generation_id)
    with pytest.raises(ArchiveRefused, match="storage") as caught:
        await store.preflight(MANIFEST, SUBJECT, REQUEST, "answer", check)
    assert closed == [True]
    assert REQUEST.query not in "".join(format_exception(caught.value))
    assert caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_internal_query_vector_binding_is_checked_before_sql_coordinates() -> None:
    session = session_mock()
    session.scalar.side_effect = [namespace(), None]
    store = DbArchiveSemanticStore(cast("Any", lambda: session), generation_id=REFERENCE.generation_id)
    wrong = (0.0, 1.0, *(0.0 for _ in range(1022)))
    with pytest.raises(ArchiveRefused, match="embedding_result"):
        await store.complete(
            MANIFEST, SUBJECT, REQUEST, "answer", SemanticPrepared(generation_version=1), execution(), wrong, check
        )
    assert session.scalar.await_count == 2
    session.add.assert_not_called()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_configured_generation_refuses_before_source_or_generation_read() -> None:
    session = session_mock()
    row = retained()
    session.scalar.side_effect = [namespace(), row]
    store = DbArchiveSemanticStore(cast("Any", lambda: session), generation_id=UUID(int=99))
    with pytest.raises(ArchiveRefused, match="receipt_binding"):
        await store.resolve(MANIFEST, SUBJECT, row.id, row.receipt_digest, "answer", check)
    assert session.scalar.await_count == 2
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_defaulted_schema_and_policy_keep_exact_receipt_bytes() -> None:
    from lychd.domain.archive.contracts import RecallReceipt

    expected = RecallReceipt(
        receipt_id=UUID(int=7),
        request_id=REQUEST.request_id,
        namespace_id=ARCHIVE.namespace_id,
        subject=SUBJECT,
        purpose="answer",
        policy_revision=ARCHIVE.policy_revision,
        query_digest=text_digest(REQUEST.query),
        request_digest="a" * 64,
        run_id=None,
        max_results=5,
        char_budget=8000,
        min_score=0.0,
        selected=(),
        omitted_for_budget=0,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
        receipt_digest="0" * 64,
    )
    expected = expected.model_copy(update={"receipt_digest": expected.computed_digest()})
    payload = expected.model_dump(mode="json", exclude={"schema_version", "profile", "retrieval_policy"})
    row = ArchiveRecall(
        id=expected.receipt_id,
        request_id=expected.request_id,
        namespace_id=expected.namespace_id,
        request_digest=expected.request_digest,
        receipt_digest=expected.receipt_digest,
        receipt=payload,
    )
    session = session_mock()
    session.scalar.side_effect = [namespace(), row]
    actual = await DbArchiveStore(cast("Any", lambda: session)).inspect_recall(
        ARCHIVE,
        RecallRequest(namespace_id=ARCHIVE.namespace_id, request_id=REQUEST.request_id, query=REQUEST.query),
        check,
    )
    assert actual is not None
    assert actual.model_dump_json() == expected.model_dump_json()
    assert actual.receipt_digest == expected.receipt_digest
