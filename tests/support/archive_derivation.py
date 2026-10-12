"""Deterministic Core lease seam for Archive persistence tests; no model runtime."""

# ruff: noqa: INP001 - existing tests/support namespace is intentional

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

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
from lychd.domain.archive.contracts import text_digest
from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE


class Embeddings:
    """Pause before the result or fail during lease cleanup under test control."""

    def __init__(self) -> None:
        self.lease_entered = asyncio.Event()
        self.claim_release = asyncio.Event()
        self.claim_release.set()
        self.calls = 0
        self.leases = 0
        self.closed = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.on_enter: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None
        self.fail_close = False
        self.transform: Callable[[EmbeddingBatch], EmbeddingBatch] = lambda result: result

    @asynccontextmanager
    async def lease_embedding(
        self, binding: EmbeddingBinding, *, operation: EmbeddingOperation, run_id: str, priority: int = 50
    ) -> AsyncGenerator[EmbeddingGrant]:
        assert binding.profile_digest == PROFILE.digest
        assert operation == "embed_document"
        assert run_id
        assert priority == 50
        self.leases += 1
        self.on_enter()
        self.lease_entered.set()
        await self.claim_release.wait()
        try:
            yield cast("EmbeddingGrant", SimpleNamespace(driver=self))
        finally:
            self.closed += 1
            self.on_close()
            if self.fail_close:
                msg = "fixture cleanup failure"
                raise RuntimeError(msg)

    async def embed(self, inputs: tuple[EmbeddingInput, ...], *, authorize: Callable[[], None]) -> EmbeddingBatch:
        authorize()
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        authorize()
        item = inputs[0]
        vector = (1.0, -0.0, *(0.0 for _ in range(1022)))
        vector_digest = embedding_vector_digest(vector)
        rendered = text_digest(PROFILE.document_prefix + item.text)
        metadata = {
            "item_id": item.item_id,
            "input_digest": item.input_digest,
            "rendered_digest": rendered,
            "vector_digest": vector_digest,
            "tokens": 3,
        }
        receipt = embedding_digest({"profile": PROFILE.digest, "operation": "embed_document", "items": [metadata]})
        return self.transform(
            EmbeddingBatch(
                PROFILE.digest,
                "embed_document",
                (EmbeddingVector(item.item_id, item.input_digest, rendered, vector, 3, vector_digest),),
                receipt,
            )
        )
