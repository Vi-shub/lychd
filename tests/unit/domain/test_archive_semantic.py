"""Effect-free semantic authority, exact request and counted-lease lifecycle checks."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from lychd.domain.animation.embeddings import (
    EmbeddingBatch,
    EmbeddingBinding,
    EmbeddingDriver,
    EmbeddingGrant,
    EmbeddingInput,
    EmbeddingOperation,
    EmbeddingProfile,
    EmbeddingVector,
    embedding_digest,
    embedding_vector_digest,
)
from lychd.domain.archive.contracts import (
    ArchiveAuthorization,
    ArchiveProfile,
    ArchiveRefused,
    ArchiveSubject,
    RecallReceipt,
    RecordRef,
    digest,
    text_digest,
)
from lychd.domain.archive.derivation import GenerationManifest, GenerationRef
from lychd.domain.archive.semantic import (
    SemanticCoverage,
    SemanticPrepared,
    SemanticQueryExecution,
    SemanticRecallItem,
    SemanticRecallProjection,
    SemanticRecallReceipt,
    SemanticRecallRequest,
    SemanticSelection,
)
from lychd.domain.archive.semantic_service import ArchiveSemanticService

if TYPE_CHECKING:
    from lychd.domain.archive.ports import AuthorizationCheck

SUBJECT = ArchiveSubject(identity="operator")
PROFILE = ArchiveProfile(namespace_id=uuid4(), owner_subject=SUBJECT, policy_revision="public-notes.1")
EMBEDDING = EmbeddingProfile(
    profile_id="unit-embedding@1",
    model_id="unit-model",
    model_sha256="a" * 64,
    image_digest="unit-image",
    engine_revision="unit-revision",
    dialect="unit-dialect",
    conformance_digest="b" * 64,
    query_prefix="Query: ",
    tokenizer="gguf:add_special=true:parse_special=true",
    pooling="last",
    normalization="l2",
    scalar_encoding="float32-le",
    dimension=1024,
    max_input_bytes=4096,
    max_input_tokens=511,
    max_response_bytes=65536,
    timeout_seconds=40,
)
MANIFEST = GenerationManifest(
    archive=PROFILE,
    binding=EmbeddingBinding(capability_key="unit:embedding", profile_digest=EMBEDDING.digest),
    embedding=EMBEDDING,
)
REFERENCE = GenerationRef(generation_id=uuid4(), manifest_digest=MANIFEST.digest)
PURPOSE = "archive:answer"
VECTOR = (1.0, -0.0, *(0.0 for _ in range(1022)))


def request(**updates: Any) -> SemanticRecallRequest:
    return SemanticRecallRequest.model_validate(
        {
            "namespace_id": PROFILE.namespace_id,
            "request_id": uuid4(),
            "generation": REFERENCE,
            "query": "  exact é\nquery  ",
            "min_cosine": -0.5,
            **updates,
        }
    )


def native_batch(expected: SemanticRecallRequest) -> EmbeddingBatch:
    metadata = {
        "item_id": str(expected.request_id),
        "input_digest": text_digest(expected.query),
        "rendered_digest": text_digest(EMBEDDING.query_prefix + expected.query),
        "vector_digest": embedding_vector_digest(VECTOR),
        "tokens": 3,
    }
    return EmbeddingBatch(
        EMBEDDING.digest,
        "embed_query",
        (
            EmbeddingVector(
                str(expected.request_id),
                text_digest(expected.query),
                text_digest(EMBEDDING.query_prefix + expected.query),
                VECTOR,
                3,
                embedding_vector_digest(VECTOR),
            ),
        ),
        embedding_digest({"profile": EMBEDDING.digest, "operation": "embed_query", "items": [metadata]}),
    )


def execution(expected: SemanticRecallRequest) -> SemanticQueryExecution:
    batch = native_batch(expected)
    item = batch.items[0]
    return SemanticQueryExecution(
        execution_id=uuid4(),
        grant_id=uuid4(),
        profile_digest=batch.profile_digest,
        item_id=item.item_id,
        input_digest=item.input_digest,
        rendered_digest=item.rendered_digest,
        vector_digest=item.vector_digest,
        tokens=item.tokens,
        receipt_digest=batch.receipt_digest,
    )


def receipt(
    expected: SemanticRecallRequest,
    observed: SemanticQueryExecution | None = None,
    **updates: Any,
) -> SemanticRecallReceipt:
    values: dict[str, Any] = {
        "schema_version": 2,
        "receipt_id": uuid4(),
        "request_id": expected.request_id,
        "namespace_id": expected.namespace_id,
        "subject": SUBJECT,
        "purpose": PURPOSE,
        "profile": PROFILE.profile,
        "retrieval_policy": "postgres-exact-cosine@1",
        "policy_revision": PROFILE.policy_revision,
        "query_digest": text_digest(expected.query),
        "request_digest": expected.binding_digest(MANIFEST, SUBJECT, PURPOSE),
        "run_id": expected.run_id,
        "max_results": expected.max_results,
        "char_budget": expected.char_budget,
        "min_cosine": expected.min_cosine,
        "generation": expected.generation.model_dump(mode="json"),
        "generation_version": 4,
        "query_execution": (observed or execution(expected)).model_dump(mode="json"),
        "coverage": {"promoted_notes": 2, "eligible_notes": 0},
        "selected": (),
        "omitted_for_budget": 0,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        **updates,
    }
    # Use JSON values so the helper exercises the retained serialization contract.
    values = {key: str(value) if isinstance(value, UUID) else value for key, value in values.items()}
    values["subject"] = (
        values["subject"].model_dump(mode="json")
        if isinstance(values["subject"], ArchiveSubject)
        else values["subject"]
    )
    return SemanticRecallReceipt.model_validate({**values, "receipt_digest": digest(values)})


class Authority:
    def __init__(self) -> None:
        self.allowed = True
        self.observed: list[ArchiveAuthorization] = []

    def authorize(self, request: ArchiveAuthorization) -> bool:
        self.observed.append(request)
        return self.allowed and request.subject == SUBJECT


class Core(EmbeddingDriver):
    def __init__(self, expected: SemanticRecallRequest) -> None:
        self.expected = expected
        self.calls = 0
        self.leases = 0
        self.closed = 0
        self.active = False
        self.inputs: tuple[EmbeddingInput, ...] = ()
        self.grant_id = uuid4()
        self.on_enter: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None
        self.on_embed: Callable[[], None] = lambda: None
        self.fail_close = False
        self.error: BaseException | None = None
        self.transform: Callable[[EmbeddingBatch], EmbeddingBatch] = lambda value: value
        self.grant_overrides: dict[str, Any] = {}
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __aenter__(self) -> Self:
        self.active = True
        self.on_enter()
        return self

    async def __aexit__(self, *args: object) -> None:
        self.active = False
        self.closed += 1
        self.on_close()
        if self.fail_close:
            msg = "fixture lease cleanup failed"
            raise RuntimeError(msg)

    @asynccontextmanager
    async def lease_embedding(
        self, binding: EmbeddingBinding, *, operation: EmbeddingOperation, run_id: str, priority: int = 50
    ) -> AsyncGenerator[EmbeddingGrant]:
        assert binding == MANIFEST.binding
        assert operation == "embed_query"
        assert run_id
        assert priority == 50
        self.leases += 1
        async with self:
            yield cast(
                "EmbeddingGrant",
                SimpleNamespace(
                    **{
                        "binding": binding,
                        "operation": operation,
                        "spec": SimpleNamespace(key=binding.capability_key, embedding_profile=EMBEDDING),
                        "lease": SimpleNamespace(grant_id=self.grant_id.hex),
                        "driver": self,
                        **self.grant_overrides,
                    }
                ),
            )

    async def embed(self, inputs: tuple[EmbeddingInput, ...], *, authorize: Callable[[], None]) -> EmbeddingBatch:
        authorize()
        self.on_embed()
        authorize()
        self.inputs = inputs
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.transform(native_batch(self.expected))


class Store:
    """Component fake: this proves no SQL, race, coverage or retirement behavior."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.retained: SemanticRecallReceipt | None = None
        self.winner: SemanticRecallReceipt | None = None
        self.preflights = 0
        self.completions = 0
        self.inspections = 0
        self.resolutions = 0
        self.on_preflight: Callable[[], None] = lambda: None
        self.on_complete: Callable[[], None] = lambda: None
        self.on_inspect: Callable[[], None] = lambda: None
        self.on_resolve: Callable[[], None] = lambda: None
        self.projection: SemanticRecallProjection | None = None

    async def preflight(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        request: SemanticRecallRequest,
        purpose: str,
        check: AuthorizationCheck,
    ) -> SemanticPrepared | SemanticRecallReceipt:
        assert not self.core.active
        self.preflights += 1
        check(request.request_id, request.binding_digest(manifest, subject, purpose))
        self.on_preflight()
        return self.retained or SemanticPrepared(generation_version=4)

    async def complete(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        request: SemanticRecallRequest,
        purpose: str,
        prepared: SemanticPrepared,
        execution: SemanticQueryExecution,
        vector: tuple[float, ...],
        check: AuthorizationCheck,
    ) -> SemanticRecallReceipt:
        assert not self.core.active
        assert self.core.closed == self.core.leases
        assert prepared.generation_version == 4
        assert vector == VECTOR
        assert math.copysign(1, vector[1]) == -1
        self.completions += 1
        check(request.request_id, request.binding_digest(manifest, subject, purpose))
        self.retained = self.winner or receipt(request, execution)
        self.on_complete()
        return self.retained

    async def inspect(
        self,
        manifest: GenerationManifest,
        request: SemanticRecallRequest,
        check: AuthorizationCheck,
    ) -> SemanticRecallReceipt | None:
        assert manifest == MANIFEST
        check(request.request_id, request.binding_digest(manifest, SUBJECT, PURPOSE))
        self.inspections += 1
        self.on_inspect()
        return self.retained

    async def resolve(
        self,
        manifest: GenerationManifest,
        subject: ArchiveSubject,
        receipt_id: UUID,
        receipt_digest: str,
        purpose: str,
        check: AuthorizationCheck,
    ) -> SemanticRecallProjection:
        assert manifest == MANIFEST
        assert subject == SUBJECT
        assert purpose == PURPOSE
        self.resolutions += 1
        check(receipt_id, receipt_digest)
        self.on_resolve()
        assert self.projection is not None
        return self.projection


def setup(expected: SemanticRecallRequest) -> tuple[ArchiveSemanticService, Authority, Store, Core]:
    core = Core(expected)
    store = Store(core)
    authority = Authority()
    service = ArchiveSemanticService(
        generation_id=REFERENCE.generation_id,
        manifest=MANIFEST,
        store=store,
        authorizer=authority,
        dispatcher=core,
    )
    return service, authority, store, core


@pytest.mark.parametrize(
    "updates",
    [
        {"query": " "},
        {"query": "\x00"},
        {"query": "\ud800"},
        {"query": "x" * 2001},
        {"min_cosine": float("nan")},
        {"min_cosine": float("inf")},
        {"min_cosine": 1.001},
        {"min_cosine": -1.001},
        {"max_results": True},
        {"unknown": "bag"},
    ],
)
def test_request_refuses_unadmitted_inputs(updates: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, UnicodeEncodeError)):
        request(**updates)


def test_explicit_threshold_and_raw_query_binding() -> None:
    expected = request()
    raw = expected.model_dump(mode="json")
    del raw["min_cosine"]
    with pytest.raises(ValidationError):
        SemanticRecallRequest.model_validate(raw)
    assert expected.query not in repr(expected)
    assert expected.model_dump()["query"] == expected.query
    assert expected.binding_digest(MANIFEST, SUBJECT, PURPOSE) != expected.model_copy(
        update={"query": expected.query.strip()}
    ).binding_digest(MANIFEST, SUBJECT, PURPOSE)


@pytest.mark.asyncio
async def test_native_query_then_exact_replay_without_model_or_source() -> None:
    expected = request(run_id=uuid4())
    service, authority, store, core = setup(expected)
    result = await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert core.inputs == (EmbeddingInput(str(expected.request_id), text_digest(expected.query), expected.query),)
    assert result.current_query_execution is not None
    assert result.current_query_execution.grant_id == core.grant_id
    assert result.current_query_execution == result.receipt.query_execution
    assert core.calls == core.closed == store.completions == 1
    replayed = await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert replayed.receipt == result.receipt
    assert replayed.current_query_execution is None
    assert core.calls == core.leases == 1
    assert store.completions == 1
    assert expected.query not in result.receipt.model_dump_json()
    assert all(item.action == "semantic_recall" for item in authority.observed)
    with pytest.raises(ValidationError):
        RecallReceipt.model_validate_json(result.receipt.model_dump_json())


@pytest.mark.asyncio
async def test_concurrent_winner_does_not_erase_callers_actual_execution() -> None:
    expected = request()
    service, _, store, core = setup(expected)
    store.winner = receipt(expected)
    result = await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert result.receipt == store.winner
    assert result.current_query_execution is not None
    assert result.current_query_execution.grant_id == core.grant_id
    assert result.current_query_execution.execution_id != result.receipt.query_execution.execution_id
    assert core.calls == store.completions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["authority", "subject", "namespace", "generation", "manifest"])
async def test_refusal_before_store_and_model_effects(bad: str) -> None:
    expected = request()
    service, authority, store, core = setup(expected)
    subject = SUBJECT
    if bad == "authority":
        authority.allowed = False
    elif bad == "subject":
        subject = ArchiveSubject(identity="foreign")
    elif bad == "namespace":
        expected = expected.model_copy(update={"namespace_id": uuid4()})
    else:
        generation = (
            REFERENCE.model_copy(update={"generation_id": uuid4()})
            if bad == "generation"
            else REFERENCE.model_copy(update={"manifest_digest": "0" * 64})
        )
        expected = expected.model_copy(update={"generation": generation})
    with pytest.raises(ArchiveRefused):
        await service.recall(subject, expected, purpose=PURPOSE)
    assert store.preflights == store.completions == core.calls == core.leases == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"subject": ArchiveSubject(identity="foreign")},
        {"purpose": "other"},
        {"policy_revision": "other"},
        {"request_digest": "0" * 64},
        {"run_id": uuid4()},
        {"min_cosine": 0.5},
        {"char_budget": 99},
        {"max_results": 2},
        {"namespace_id": uuid4()},
        {"generation": GenerationRef(generation_id=uuid4(), manifest_digest=MANIFEST.digest).model_dump(mode="json")},
    ],
)
async def test_retained_replay_refuses_whole_binding_even_with_valid_outer_digest(change: dict[str, Any]) -> None:
    expected = request()
    service, _, store, core = setup(expected)
    store.retained = receipt(expected, **change)
    with pytest.raises(ArchiveRefused, match="receipt_binding"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert core.leases == core.calls == store.completions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["preflight", "lease", "wire", "result", "cleanup", "complete"])
async def test_current_authority_rechecked_after_every_handoff(boundary: str) -> None:
    expected = request()
    service, authority, store, core = setup(expected)

    def revoke() -> None:
        authority.allowed = False

    if boundary == "preflight":
        store.on_preflight = revoke
    elif boundary == "lease":
        core.on_enter = revoke
    elif boundary == "wire":
        core.on_embed = revoke
    elif boundary == "result":

        def revoke_result(batch: EmbeddingBatch) -> EmbeddingBatch:
            revoke()
            return batch

        core.transform = revoke_result
    elif boundary == "cleanup":
        core.on_close = revoke
    else:
        store.on_complete = revoke
    with pytest.raises(ArchiveRefused, match="authority"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert not core.active
    assert core.closed == core.leases
    assert store.completions == int(boundary == "complete")
    if boundary in {"preflight", "lease", "wire"}:
        assert core.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "profile",
        "operation",
        "item",
        "input",
        "rendered",
        "vector_digest",
        "dimension",
        "norm",
        "nan",
        "tokens",
        "receipt",
        "precision",
        "usage",
    ],
)
async def test_malformed_native_output_never_completes(failure: str) -> None:
    expected = request()
    service, _, store, core = setup(expected)

    def corrupt(batch: EmbeddingBatch) -> EmbeddingBatch:
        item = batch.items[0]
        if failure == "profile":
            return replace(batch, profile_digest="0" * 64)
        if failure == "operation":
            return replace(batch, operation="embed_document")
        if failure == "receipt":
            return replace(batch, receipt_digest="0" * 64)
        fields: dict[str, dict[str, Any]] = {
            "item": {"item_id": str(uuid4())},
            "input": {"input_digest": "0" * 64},
            "rendered": {"rendered_digest": "0" * 64},
            "vector_digest": {"vector_digest": "0" * 64},
            "dimension": {"vector": (1.0,)},
            "norm": {"vector": (0.0,) * 1024},
            "nan": {"vector": (float("nan"), *VECTOR[1:])},
            "tokens": {"tokens": True},
            "usage": {"tokens": 512},
            "precision": {"vector": (1.0000000001, *VECTOR[1:])},
        }
        return replace(batch, items=(replace(item, **fields[failure]),))

    core.transform = corrupt
    with pytest.raises(ValueError, match="refused"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert core.calls == core.closed == 1
    assert not core.active
    assert store.completions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cleanup", "timeout", "cancellation"])
async def test_cleanup_failure_timeout_and_cancellation_never_publish(failure: str) -> None:
    expected = request()
    service, _, store, core = setup(expected)
    if failure == "cleanup":
        core.fail_close = True
        with pytest.raises(RuntimeError, match="cleanup"):
            await service.recall(SUBJECT, expected, purpose=PURPOSE)
    elif failure == "timeout":
        core.error = TimeoutError()
        with pytest.raises(TimeoutError):
            await service.recall(SUBJECT, expected, purpose=PURPOSE)
    else:
        core.release.clear()
        task = asyncio.create_task(service.recall(SUBJECT, expected, purpose=PURPOSE))
        await core.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert core.closed == core.leases == 1
    assert not core.active
    assert store.completions == 0
    assert store.retained is None


@pytest.mark.asyncio
async def test_utf8_rendered_byte_bound_before_query_disclosure() -> None:
    expected = request(query="界" * 2000)
    service, _, store, core = setup(expected)
    with pytest.raises(ArchiveRefused, match="embedding_input_size"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert store.preflights == 1
    assert core.leases == core.calls == store.completions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_inspect_is_source_free_and_rechecks_after_cleanup(*, revoke: bool) -> None:
    expected = request()
    service, authority, store, core = setup(expected)
    store.retained = receipt(expected)
    store.on_inspect = lambda: setattr(authority, "allowed", not revoke)
    if revoke:
        with pytest.raises(ArchiveRefused, match="authority"):
            await service.inspect_recall(SUBJECT, expected, recall_purpose=PURPOSE, purpose="manage")
    else:
        assert (
            await service.inspect_recall(SUBJECT, expected, recall_purpose=PURPOSE, purpose="manage") == store.retained
        )
    assert store.inspections == 1
    assert store.resolutions == store.preflights == core.leases == 0
    assert all(item.action == "inspect" and item.purpose == "manage" for item in authority.observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_resolver_final_authority_after_store_cleanup(*, revoke: bool) -> None:
    expected = request()
    service, authority, store, core = setup(expected)
    retained = receipt(expected)
    store.projection = SemanticRecallProjection(receipt=retained, items=())
    store.on_resolve = lambda: setattr(authority, "allowed", not revoke)
    if revoke:
        with pytest.raises(ArchiveRefused, match="authority"):
            await service.resolve_receipt(SUBJECT, retained.receipt_id, retained.receipt_digest, purpose=PURPOSE)
    else:
        assert (
            await service.resolve_receipt(SUBJECT, retained.receipt_id, retained.receipt_digest, purpose=PURPOSE)
            == store.projection
        )
    assert core.leases == core.calls == store.preflights == 0
    assert all(item.action == "semantic_resolve" for item in authority.observed)


def test_receipt_structure_projection_and_integrity_refuse_incoherence() -> None:
    expected = request()
    selected = SemanticSelection(
        reference=RecordRef(record_id=uuid4(), revision_digest="c" * 64, lifecycle_version=1),
        source_key="unit-note",
        source_revision="1",
        source_digest=text_digest("exact source"),
        score=0.8,
        projection_digest=text_digest("exact source"),
        projected_chars=12,
        derivation_id=uuid4(),
        row_version=1,
        vector_digest=embedding_vector_digest(VECTOR),
    )
    retained = receipt(
        expected, coverage={"promoted_notes": 2, "eligible_notes": 1}, selected=[selected.model_dump(mode="json")]
    )
    item = SemanticRecallItem(selection=selected, text="exact source")
    assert SemanticRecallProjection(receipt=retained, items=(item,)).items == (item,)
    with pytest.raises(ValidationError):
        SemanticRecallProjection(receipt=retained, items=())
    with pytest.raises(ValidationError):
        SemanticRecallItem(selection=selected, text="changed text")
    with pytest.raises(ValidationError):
        SemanticCoverage(promoted_notes=0, eligible_notes=1)
    with pytest.raises(ValidationError):
        SemanticRecallReceipt.model_validate({**retained.model_dump(mode="json"), "receipt_digest": "0" * 64})
    with pytest.raises(ValidationError):
        receipt(
            expected,
            coverage={"promoted_notes": 2, "eligible_notes": 1},
            selected=[selected.model_dump(mode="json")] * 2,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["binding", "operation", "profile", "capability", "grant_id"])
async def test_malformed_grant_refuses_before_native_query(failure: str) -> None:
    expected = request()
    service, _, store, core = setup(expected)
    overrides: dict[str, dict[str, Any]] = {
        "binding": {"binding": MANIFEST.binding.model_copy(update={"profile_digest": "0" * 64})},
        "operation": {"operation": "embed_document"},
        "profile": {"spec": SimpleNamespace(key=MANIFEST.binding.capability_key, embedding_profile=None)},
        "capability": {"spec": SimpleNamespace(key="other", embedding_profile=EMBEDDING)},
        "grant_id": {"lease": SimpleNamespace(grant_id="invalid")},
    }
    core.grant_overrides = overrides[failure]
    with pytest.raises(ArchiveRefused, match="embedding_grant"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert core.calls == store.completions == 0
    assert core.closed == core.leases == 1
    assert not core.active


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"query": "changed query"},
        {"request_id": uuid4()},
        {"run_id": uuid4()},
        {"min_cosine": 0.3},
        {"max_results": 2},
        {"char_budget": 200},
    ],
)
async def test_changed_expected_request_cannot_replay_or_inspect_retained_selection(change: dict[str, Any]) -> None:
    expected = request()
    service, _, store, core = setup(expected)
    store.retained = receipt(expected)
    changed = expected.model_copy(update=change)
    with pytest.raises(ArchiveRefused, match="receipt_binding"):
        await service.recall(SUBJECT, changed, purpose=PURPOSE)
    with pytest.raises(ArchiveRefused, match="receipt_binding"):
        await service.inspect_recall(SUBJECT, changed, recall_purpose=PURPOSE, purpose="manage")
    assert core.leases == core.calls == store.completions == 0


@pytest.mark.asyncio
async def test_malformed_retained_receipt_cannot_bypass_model_validation() -> None:
    expected = request()
    service, _, store, core = setup(expected)
    store.retained = receipt(expected).model_copy(update={"receipt_digest": "0" * 64})
    with pytest.raises(ArchiveRefused, match="receipt_integrity"):
        await service.recall(SUBJECT, expected, purpose=PURPOSE)
    assert core.leases == core.calls == store.completions == 0


@pytest.mark.parametrize("mutation", ["order", "threshold", "characters", "naive_time", "count"])
def test_receipt_cross_field_constraints(mutation: str) -> None:
    expected = request(max_results=2, char_budget=20, min_cosine=0.5)

    def selected(record_id: int, score: float) -> dict[str, Any]:
        return SemanticSelection(
            reference=RecordRef(record_id=UUID(int=record_id), revision_digest="c" * 64, lifecycle_version=1),
            source_key=f"unit-note-{record_id}",
            source_revision="1",
            source_digest=text_digest("exact source"),
            score=score,
            projection_digest=text_digest("exact source"),
            projected_chars=12,
            derivation_id=uuid4(),
            row_version=1,
            vector_digest=embedding_vector_digest(VECTOR),
        ).model_dump(mode="json")

    updates: dict[str, Any] = {"coverage": {"promoted_notes": 2, "eligible_notes": 2}}
    if mutation == "order":
        updates.update(selected=[selected(2, 0.8), selected(1, 0.8)], char_budget=30)
    elif mutation == "threshold":
        updates.update(selected=[selected(1, 0.4)])
    elif mutation == "characters":
        updates.update(selected=[selected(1, 0.8), selected(2, 0.6)])
    elif mutation == "naive_time":
        updates.update(created_at="2026-10-19T00:00:00")
    else:
        updates.update(selected=[selected(1, 0.8)], omitted_for_budget=2)
    with pytest.raises(ValidationError):
        receipt(expected, **updates)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_results", True),
        ("max_results", "2"),
        ("max_results", 2.0),
        ("char_budget", True),
        ("char_budget", "20"),
        ("char_budget", 20.0),
        ("min_cosine", True),
        ("min_cosine", "0.25"),
    ],
)
def test_semantic_numeric_identity_survives_lax_transport_validation(field: str, value: object) -> None:
    raw = request().model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        SemanticRecallRequest.model_validate(raw, strict=False)


@pytest.mark.parametrize("threshold", [-1, 0, 1])
def test_integer_json_cosine_values_remain_valid(threshold: int) -> None:
    raw = request().model_dump(mode="json")
    raw["min_cosine"] = threshold
    assert SemanticRecallRequest.model_validate(raw, strict=False).min_cosine == float(threshold)
