"""Typed, lease-scoped embedding execution independent of Archive persistence."""

from __future__ import annotations

import json
import math
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType
    from typing import Self

    from lychd.domain.animation.capabilities import CapabilitySpec, CapabilityState, GrantLease

EmbeddingOperation = Literal["embed_document", "embed_query"]


def embedding_digest(value: object) -> str:
    """Bind this interface's deterministic finite JSON representation."""
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class EmbeddingRefused(ValueError):  # noqa: N818 - domain refusal signal
    """Safe failure code without source bytes, provider body or endpoint details."""

    def __init__(self, code: str) -> None:
        """Retain only the safe refusal code."""
        self.code = code
        super().__init__(f"Embedding refused: {code}.")


class EmbeddingProfile(BaseModel):
    """Immutable representation and execution contract; identity is its full digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str
    interface: Literal["model.embed@1"] = "model.embed@1"
    model_id: str
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str
    engine_revision: str
    dialect: str
    conformance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_prefix: str
    document_prefix: str = ""
    tokenizer: Literal["gguf:add_special=true:parse_special=true"]
    pooling: Literal["last"]
    normalization: Literal["l2"]
    normalization_tolerance: float = Field(default=0.0001, ge=0.0001, le=0.0001)
    scalar_encoding: Literal["float32-le"]
    dimension: int = Field(gt=0)
    max_batch_items: Literal[1] = 1
    max_input_bytes: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_response_bytes: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)

    @property
    def digest(self) -> str:
        """Return the exact versioned representation signature."""
        return embedding_digest(self.model_dump(mode="json"))


class EmbeddingBinding(BaseModel):
    """Exact compatibility projection, never a floating model-name selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    capability_key: str = Field(min_length=1)
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    interface: Literal["model.embed@1"] = "model.embed@1"


@dataclass(frozen=True, slots=True)
class EmbeddingInput:
    """Transient source bytes with caller-owned stable identity and digest."""

    item_id: str
    input_digest: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Validated indexed output without retaining source text."""

    item_id: str
    input_digest: str
    rendered_digest: str
    vector: tuple[float, ...] = field(repr=False)
    tokens: int
    vector_digest: str


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Safe handoff to a caller's durable derivation acceptance fence."""

    profile_digest: str
    operation: EmbeddingOperation
    items: tuple[EmbeddingVector, ...]
    receipt_digest: str


class EmbeddingDriver(ABC):
    """One admitted operation, with provider lifetime owned by Dispatcher."""

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Enter the lease-scoped lifetime; failed or cancelled entry must self-clean."""
        ...

    @abstractmethod
    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        """Join outstanding work and release transport resources."""
        ...

    @abstractmethod
    async def embed(self, inputs: tuple[EmbeddingInput, ...], *, authorize: Callable[[], None]) -> EmbeddingBatch:
        """Reauthorize each source-bearing wire; never keep source bytes in output."""
        ...


@dataclass(frozen=True, slots=True)
class EmbeddingGrant:
    """Exact operation/profile handoff counted by the ordinary lease ledger."""

    spec: CapabilitySpec
    state: CapabilityState
    lease: GrantLease
    binding: EmbeddingBinding
    operation: EmbeddingOperation
    driver: EmbeddingDriver


def canonical_float32_vector(values: tuple[float, ...]) -> tuple[float, ...]:
    """Project finite scalars exactly to storage precision without normalization."""
    try:
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in values):
            raise EmbeddingRefused("vector_scalar")  # noqa: EM101 - closed machine-readable refusal code
        projected = struct.unpack(f"<{len(values)}f", struct.pack(f"<{len(values)}f", *values))
    except (OverflowError, struct.error):
        raise EmbeddingRefused("vector_scalar") from None  # noqa: EM101 - closed machine-readable refusal code
    if not all(math.isfinite(v) for v in projected):
        raise EmbeddingRefused("vector_scalar")  # noqa: EM101 - closed machine-readable refusal code
    return projected


def embedding_vector_digest(values: tuple[float, ...]) -> str:
    """Hash canonical little-endian float32 bytes used by pgvector storage."""
    canonical = canonical_float32_vector(values)
    return sha256(struct.pack(f"<{len(canonical)}f", *canonical)).hexdigest()
