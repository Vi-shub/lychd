"""Actual PostgreSQL exact semantic component tests; deterministic leases are not model proof."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lychd.config.settings import Settings
from lychd.db.archive_derivation import DbArchiveDerivationStore
from lychd.db.archive_semantic import DbArchiveSemanticStore
from lychd.db.factory import create_db_engine
from lychd.db.models.archive import ArchiveNamespace, ArchiveRecall
from lychd.db.models.archive_derivation import ArchiveNoteEmbedding
from lychd.domain.animation.embeddings import (
    EmbeddingBatch,
    EmbeddingBinding,
    EmbeddingGrant,
    EmbeddingInput,
    EmbeddingOperation,
    EmbeddingVector,
    embedding_digest,
    embedding_vector_digest,
)
from lychd.domain.archive import ArchiveRefused, RecallRequest, text_digest
from lychd.domain.archive.derivation import DerivationRequest
from lychd.domain.archive.derivation_service import ArchiveDerivationService
from lychd.domain.archive.semantic import SemanticRecallRequest
from lychd.domain.archive.semantic_service import ArchiveSemanticService
from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE
from tests.integration import test_archive_derivation_pg as derivations
from tests.integration import test_archive_pg as notes
from tests.integration import test_database_authority_pg as runtime_authority
from tests.support.archive_derivation import Embeddings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.container]
pg_url = notes.pg_url
factory = notes.factory
database = runtime_authority.database
UNIT = (1.0, *(0.0 for _ in range(1023)))
NEGATIVE = (-1.0, *(0.0 for _ in range(1023)))
ORTHOGONAL = (0.0, 1.0, *(0.0 for _ in range(1022)))


def batch(operation: EmbeddingOperation, item: EmbeddingInput, vector: tuple[float, ...]) -> EmbeddingBatch:
    rendered = text_digest(
        (PROFILE.query_prefix if operation == "embed_query" else PROFILE.document_prefix) + item.text
    )
    value = EmbeddingVector(item.item_id, item.input_digest, rendered, vector, 3, embedding_vector_digest(vector))
    metadata = {
        "item_id": value.item_id,
        "input_digest": value.input_digest,
        "rendered_digest": value.rendered_digest,
        "vector_digest": value.vector_digest,
        "tokens": value.tokens,
    }
    return EmbeddingBatch(
        PROFILE.digest,
        operation,
        (value,),
        embedding_digest({"profile": PROFILE.digest, "operation": operation, "items": [metadata]}),
    )


class Queries:
    def __init__(self) -> None:
        self.calls = self.closed = 0
        self.vector = UNIT
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.on_close: Callable[[], None] = lambda: None

    @asynccontextmanager
    async def lease_embedding(
        self, binding: EmbeddingBinding, *, operation: EmbeddingOperation, run_id: str, priority: int = 50
    ) -> AsyncGenerator[EmbeddingGrant]:
        assert operation == "embed_query"
        assert binding.profile_digest == PROFILE.digest
        assert run_id
        assert priority == 50
        try:
            yield cast(
                "EmbeddingGrant",
                SimpleNamespace(
                    binding=binding,
                    operation=operation,
                    spec=SimpleNamespace(key=binding.capability_key, embedding_profile=PROFILE),
                    lease=SimpleNamespace(grant_id=str(uuid4())),
                    driver=self,
                ),
            )
        finally:
            self.closed += 1
            self.on_close()

    async def embed(self, inputs: tuple[EmbeddingInput, ...], *, authorize: Callable[[], None]) -> EmbeddingBatch:
        authorize()
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        authorize()
        return batch("embed_query", inputs[0], self.vector)


def consumer(
    factory: async_sessionmaker[AsyncSession],
    authority: notes.Authority,
    generation: ArchiveDerivationService,
    queries: Queries,
) -> ArchiveSemanticService:
    return ArchiveSemanticService(
        generation_id=generation.reference.generation_id,
        manifest=generation.manifest,
        store=DbArchiveSemanticStore(factory, generation_id=generation.reference.generation_id),
        authorizer=authority,
        dispatcher=queries,
    )


def request(generation: ArchiveDerivationService, **updates: object) -> SemanticRecallRequest:
    return SemanticRecallRequest.model_validate(
        {
            "namespace_id": generation.manifest.archive.namespace_id,
            "request_id": uuid4(),
            "generation": generation.reference,
            "query": "public semantic fixture",
            "min_cosine": -1.0,
            **updates,
        }
    )


async def document(
    factory: async_sessionmaker[AsyncSession],
    authority: notes.Authority,
    generation: ArchiveDerivationService,
    key: str,
    content: str,
    vector: tuple[float, ...] = UNIT,
) -> UUID:
    archive = notes.service(factory, authority)
    note = await notes.promote(
        archive, notes.proposal(namespace_id=authority.profile.namespace_id, source_key=key, text=content)
    )
    driver = Embeddings()

    def transform(result: EmbeddingBatch) -> EmbeddingBatch:
        original = result.items[0]
        return batch("embed_document", EmbeddingInput(original.item_id, original.input_digest, content), vector)

    driver.transform = transform
    service = ArchiveDerivationService(
        generation_id=generation.reference.generation_id,
        manifest=generation.manifest,
        store=DbArchiveDerivationStore(factory),
        authorizer=authority,
        dispatcher=driver,
    )
    await service.derive(
        notes.SUBJECT, DerivationRequest(generation=service.reference, source=note.reference), purpose="derive"
    )
    return note.reference.record_id


async def setup(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[notes.Authority, ArchiveDerivationService, Queries, ArchiveSemanticService]:
    authority = notes.Authority()
    archive = notes.service(factory, authority)
    # Admission creates the existing namespace; semantic preflight cannot create it.
    await archive.admit(notes.SUBJECT, notes.proposal(source_key="candidate"), purpose="manage")
    generation = derivations.service(factory, authority, Embeddings())
    await generation.register(notes.SUBJECT, generation.reference, purpose="manage")
    queries = Queries()
    return authority, generation, queries, consumer(factory, authority, generation, queries)


async def test_exact_filters_negative_inclusive_threshold_uuid_ties_and_final_coverage(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, queries, semantic = await setup(factory)
    ties = [await document(factory, authority, generation, str(i), "tie note", ORTHOGONAL) for i in range(2)]
    negative = await document(factory, authority, generation, "negative", "negative note", NEGATIVE)
    stale = await document(factory, authority, generation, "stale", "incompatible high score")
    stale_source = await document(factory, authority, generation, "stale-source", "source fence high score")
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE archive_note_embedding SET generation_version=generation_version+1 WHERE note_id=:id"),
            {"id": stale},
        )
        await session.execute(
            text(
                "UPDATE archive_note_embedding SET source_lifecycle_version=source_lifecycle_version+1 WHERE note_id=:id"
            ),
            {"id": stale_source},
        )
    # A higher-scoring complete vector in another configured generation must not win.
    # Same manifest cannot register twice: use an actually distinct immutable capability binding.
    different = generation.manifest.model_copy(
        update={"binding": generation.manifest.binding.model_copy(update={"capability_key": "local:embedding:other"})}
    )
    other = ArchiveDerivationService(
        generation_id=uuid4(),
        manifest=different,
        store=DbArchiveDerivationStore(factory),
        authorizer=authority,
        dispatcher=Embeddings(),
    )
    await other.register(notes.SUBJECT, other.reference, purpose="manage")
    await document(factory, authority, other, "other", "foreign-generation high score")
    foreign = notes.Authority(authority.profile.model_copy(update={"namespace_id": uuid4()}))
    foreign_generation = derivations.service(factory, foreign, Embeddings())
    await foreign_generation.register(notes.SUBJECT, foreign_generation.reference, purpose="manage")
    await document(factory, foreign, foreign_generation, "foreign", "foreign namespace high score")
    result = await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
    assert [s.reference.record_id for s in result.receipt.selected] == [*sorted(ties), negative]
    assert [s.score for s in result.receipt.selected] == [0.0, 0.0, -1.0]
    assert result.receipt.coverage.promoted_notes == 6
    assert result.receipt.coverage.eligible_notes == 3
    boundary = await semantic.recall(notes.SUBJECT, request(generation, min_cosine=0.0), purpose="answer")
    assert [s.reference.record_id for s in boundary.receipt.selected] == sorted(ties)
    assert queries.calls == queries.closed == 2


async def test_whole_note_fit_omits_without_backfill_and_receipt_has_no_payload(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, _, semantic = await setup(factory)
    big = await document(factory, authority, generation, "big", "x" * 100)
    await document(factory, authority, generation, "small", "tiny", ORTHOGONAL)
    expected = request(generation, max_results=1, char_budget=4)
    result = await semantic.recall(notes.SUBJECT, expected, purpose="answer")
    assert result.receipt.selected == ()
    assert result.receipt.omitted_for_budget == 1
    assert result.receipt.coverage.eligible_notes == 2
    assert str(big) not in result.receipt.model_dump_json()
    assert expected.query not in result.receipt.model_dump_json()
    assert "x" * 100 not in result.receipt.model_dump_json()
    assert "vector" not in result.receipt.query_execution.model_dump(exclude={"vector_digest"})
    assert result.receipt.computed_digest() == result.receipt.receipt_digest


async def test_replay_inspection_after_retirement_and_cross_mode_conflict_before_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, queries, semantic = await setup(factory)
    await document(factory, authority, generation, "one", "source")
    expected = request(generation)
    first = await semantic.recall(notes.SUBJECT, expected, purpose="answer")
    await generation.retire(notes.SUBJECT, generation.reference, expected_version=1, purpose="manage")
    replay = await semantic.recall(notes.SUBJECT, expected, purpose="answer")
    assert replay.receipt == first.receipt
    assert replay.current_query_execution is None
    assert queries.calls == 1
    assert (
        await semantic.inspect_recall(notes.SUBJECT, expected, recall_purpose="answer", purpose="manage")
        == first.receipt
    )
    with pytest.raises(ArchiveRefused, match="generation_retired"):
        await semantic.resolve_receipt(
            notes.SUBJECT, first.receipt.receipt_id, first.receipt.receipt_digest, purpose="answer"
        )
    lexical = notes.service(factory, authority)
    with pytest.raises(ArchiveRefused, match="receipt_schema"):
        await lexical.resolve_receipt(
            notes.SUBJECT, first.receipt.receipt_id, first.receipt.receipt_digest, purpose="answer"
        )
    lexical_request = RecallRequest(namespace_id=authority.profile.namespace_id, request_id=uuid4(), query="source")
    await lexical.recall(notes.SUBJECT, lexical_request, purpose="answer")
    with pytest.raises(ArchiveRefused, match="request_conflict"):
        await semantic.recall(
            notes.SUBJECT, request(generation, request_id=lexical_request.request_id), purpose="answer"
        )
    assert queries.calls == 1


async def test_concurrent_identical_queries_keep_one_receipt_and_both_executions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, first_driver, first = await setup(factory)
    second_driver = Queries()
    second = consumer(factory, authority, generation, second_driver)
    first_driver.release.clear()
    second_driver.release.clear()
    expected = request(generation)
    one = asyncio.create_task(first.recall(notes.SUBJECT, expected, purpose="answer"))
    await first_driver.entered.wait()
    two = asyncio.create_task(second.recall(notes.SUBJECT, expected, purpose="answer"))
    await second_driver.entered.wait()
    first_driver.release.set()
    winner = await one
    # A completed duplicate wins before the unpublished active-generation check.
    await generation.retire(notes.SUBJECT, generation.reference, expected_version=1, purpose="manage")
    second_driver.release.set()
    loser = await two
    assert winner.receipt == loser.receipt
    assert winner.current_query_execution is not None
    assert loser.current_query_execution is not None
    assert winner.current_query_execution.execution_id != loser.current_query_execution.execution_id
    assert first_driver.calls == first_driver.closed == second_driver.calls == second_driver.closed == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 1


@pytest.mark.parametrize("change", ["archive", "retire"])
async def test_lifecycle_changes_during_query_have_no_database_lock_and_use_final_snapshot(
    factory: async_sessionmaker[AsyncSession],
    change: str,
) -> None:
    authority, generation, queries, semantic = await setup(factory)
    note = await document(factory, authority, generation, "one", "source")
    queries.release.clear()
    pending = asyncio.create_task(semantic.recall(notes.SUBJECT, request(generation), purpose="answer"))
    await queries.entered.wait()
    if change == "archive":
        await notes.service(factory, authority).archive(notes.SUBJECT, note, expected_version=2, purpose="manage")
    else:
        await generation.retire(notes.SUBJECT, generation.reference, expected_version=1, purpose="manage")
    queries.release.set()
    if change == "retire":
        with pytest.raises(ArchiveRefused, match="generation_retired"):
            await pending
    else:
        result = await pending
        assert result.receipt.selected == ()
        assert result.receipt.coverage.promoted_notes == result.receipt.coverage.eligible_notes == 0
    assert queries.closed == 1


@pytest.mark.parametrize("field", ["vector", "receipt", "row_version", "source_digest"])
async def test_resolution_refuses_corrupt_or_changed_derivation_fence(
    factory: async_sessionmaker[AsyncSession],
    field: str,
) -> None:
    authority, generation, _, semantic = await setup(factory)
    await document(factory, authority, generation, "one", "source")
    result = await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
    selected = result.receipt.selected[0]
    async with factory() as session, session.begin():
        row = await session.get(ArchiveNoteEmbedding, selected.derivation_id)
        assert row is not None
        if field == "vector":
            row.vector = list(ORTHOGONAL)
        elif field == "row_version":
            row.row_version += 1
        elif field == "receipt":
            assert row.receipt is not None
            row.receipt = {**row.receipt, "receipt_digest": "0" * 64}
        else:
            setattr(row, field, "0" * 64)
    with pytest.raises(ArchiveRefused, match=r"source_integrity|receipt_stale"):
        await semantic.resolve_receipt(
            notes.SUBJECT, result.receipt.receipt_id, result.receipt.receipt_digest, purpose="answer"
        )


async def test_empty_eligible_set_is_distinct_from_missing_generation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, queries, semantic = await setup(factory)
    result = await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
    assert result.receipt.coverage.eligible_notes == 0
    assert result.receipt.selected == ()
    missing = derivations.service(factory, authority, Embeddings())
    unavailable = consumer(factory, authority, missing, queries)
    with pytest.raises(ArchiveRefused, match="generation_missing"):
        await unavailable.recall(notes.SUBJECT, request(missing), purpose="answer")
    assert queries.calls == 1


async def test_current_authority_after_session_cleanup_refuses_retained_response(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, queries, _ = await setup(factory)
    armed = False

    class Session(AsyncSession):
        async def __aexit__(self, *args: object) -> None:
            await super().__aexit__(*args)
            if armed:
                authority.allowed = False

    def arm() -> None:
        nonlocal armed
        armed = True

    queries.on_close = arm
    bound = async_sessionmaker(factory.kw["bind"], class_=Session, expire_on_commit=False)
    semantic = consumer(cast("async_sessionmaker[AsyncSession]", bound), authority, generation, queries)
    expected = request(generation)
    with pytest.raises(ArchiveRefused, match="authority"):
        await semantic.recall(notes.SUBJECT, expected, purpose="answer")
    authority.allowed = True
    assert (
        await consumer(factory, authority, generation, queries).inspect_recall(
            notes.SUBJECT, expected, recall_purpose="answer", purpose="manage"
        )
        is not None
    )


async def test_namespace_lock_timeout_is_typed_and_does_not_query_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, generation, queries, semantic = await setup(factory)
    async with factory() as blocker, blocker.begin():
        await blocker.scalar(
            select(ArchiveNamespace)
            .where(ArchiveNamespace.id == generation.manifest.archive.namespace_id)
            .with_for_update()
        )
        with pytest.raises(ArchiveRefused, match="storage"):
            await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
    assert queries.calls == 0


async def test_existing_restricted_runtime_role_can_select_publish_and_resolve(database: Settings) -> None:
    engine = create_db_engine(database.server.database, runtime=True)
    try:
        bound = async_sessionmaker(engine, expire_on_commit=False)
        authority, generation, _, semantic = await setup(bound)
        await document(bound, authority, generation, "semantic-runtime", "runtime public source")
        result = await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
        projection = await semantic.resolve_receipt(
            notes.SUBJECT, result.receipt.receipt_id, result.receipt.receipt_digest, purpose="answer"
        )
        assert tuple(item.text for item in projection.items) == ("runtime public source",)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", ["archive", "revoke", "supersede"])
async def test_current_source_change_refuses_resolution_but_keeps_historical_metadata(
    factory: async_sessionmaker[AsyncSession],
    change: str,
) -> None:
    authority, generation, queries, semantic = await setup(factory)
    note = await document(factory, authority, generation, "one", "source")
    expected = request(generation)
    result = await semantic.recall(notes.SUBJECT, expected, purpose="answer")
    archive = notes.service(factory, authority)
    if change == "supersede":
        await notes.promote(archive, notes.proposal(source_key="one", source_revision="2", text="corrected source"))
    else:
        await getattr(archive, change)(notes.SUBJECT, note, expected_version=2, purpose="manage")
    with pytest.raises(ArchiveRefused, match=r"source_integrity|receipt_stale"):
        await semantic.resolve_receipt(
            notes.SUBJECT, result.receipt.receipt_id, result.receipt.receipt_digest, purpose="answer"
        )
    assert (
        await semantic.inspect_recall(notes.SUBJECT, expected, recall_purpose="answer", purpose="manage")
        == result.receipt
    )
    assert (await semantic.recall(notes.SUBJECT, expected, purpose="answer")).current_query_execution is None
    assert queries.calls == 1


async def test_source_bytes_corruption_cannot_publish_a_miss(factory: async_sessionmaker[AsyncSession]) -> None:
    authority, generation, queries, semantic = await setup(factory)
    note = await document(factory, authority, generation, "one", "source")
    async with factory() as session, session.begin():
        await session.execute(text("UPDATE archive_note SET content='corrupt bytes' WHERE id=:id"), {"id": note})
    with pytest.raises(ArchiveRefused, match="source_integrity"):
        await semantic.recall(notes.SUBJECT, request(generation), purpose="answer")
    assert queries.calls == queries.closed == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0


async def test_semantic_statement_timeout_is_local_failure_not_a_miss(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    authority, generation, queries, _ = await setup(factory)
    observed: list[tuple[str, str]] = []

    class SlowSelectionSession(AsyncSession):
        async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(statement, "is_select", False) and "score" in statement.selected_columns:
                settings = await super().execute(
                    text("SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
                )
                row = settings.one()
                observed.append((str(row[0]), str(row[1])))
                await super().execute(text("SELECT pg_sleep(10)"))
            return await super().execute(statement, *args, **kwargs)

    slow = async_sessionmaker[AsyncSession](factory.kw["bind"], class_=SlowSelectionSession, expire_on_commit=False)
    with pytest.raises(ArchiveRefused, match="storage") as failure:
        await consumer(slow, authority, generation, queries).recall(
            notes.SUBJECT, request(generation), purpose="answer"
        )
    assert failure.value.__suppress_context__ is True
    assert isinstance(failure.value.__context__, DBAPIError)
    assert "statement timeout" in str(failure.value.__context__)
    assert observed == [("5s", "2s")]
    assert queries.calls == queries.closed == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ArchiveRecall)) == 0
        assert await session.scalar(text("SHOW statement_timeout")) == "0"
