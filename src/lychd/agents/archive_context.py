"""Resolve attributed Archive data at the outbound model boundary only."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.wrapper import WrapperModel

from lychd.domain.archive import ArchiveRefused, RecallProjection

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import RequestUsage


class ArchiveModel(WrapperModel):
    """Attach one currently authorized receipt as transient, untrusted user data.

    The mandatory resolver owns authorization and current lifecycle checks. It
    must resolve the same exact receipt on every call and raise on invalidation.
    This request-scoped wrapper checks the wire projection's bound and stability;
    a reference or digest grants no authority. ContextWindowModel belongs outside
    this wrapper so its usage checks include the projection.

    Only a detached outbound list receives the additional request. The Agent's
    source messages, instructions and durable history never receive that request.
    A model's response can still quote its inputs; this is not an output censor.
    """

    def __init__(
        self,
        wrapped: Model,
        *,
        resolver: Callable[[], Awaitable[RecallProjection]],
        max_chars: int,
    ) -> None:
        """Bind an exact-receipt resolver and a reserved final-envelope budget."""
        if max_chars < 1:
            msg = "Archive context requires a positive character budget."
            raise ValueError(msg)
        model = wrapped
        while isinstance(model, WrapperModel):
            if isinstance(model, ArchiveModel):
                msg = "Archive context is already attached to this model."
                raise TypeError(msg)
            model = model.wrapped
        super().__init__(wrapped)
        self._resolver = resolver
        self._max_chars = max_chars
        self._timestamp = datetime.now(UTC)
        self._wire_digest: bytes | None = None

    @property
    def max_chars(self) -> int:
        """Characters Context must reserve for the complete serialized envelope."""
        return self._max_chars

    async def projected_chars(self) -> int:
        """Resolve and pin the exact size for Context's transient reservation.

        Counting and generation still resolve independently; this observation
        does not authorize a later provider request.
        """
        return len(await self._wire())

    async def _wire(self) -> str:
        projection = await self._resolver()
        receipt = projection.receipt
        envelope = {
            "kind": "archive_recall",
            "instruction_authority": "none",
            "receipt": {
                "receipt_id": str(receipt.receipt_id),
                "receipt_digest": receipt.receipt_digest,
                "namespace_id": str(receipt.namespace_id),
                "purpose": receipt.purpose,
                "policy_revision": receipt.policy_revision,
                "retrieval_policy": receipt.retrieval_policy,
                "omitted_for_budget": receipt.omitted_for_budget,
            },
            "items": [item.model_dump(mode="json") for item in projection.items],
        }
        wire = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(wire) > self.max_chars:
            code = "projection_budget_exceeded"
            raise ArchiveRefused(code)
        wire_digest = sha256(wire.encode()).digest()
        if self._wire_digest is not None and wire_digest != self._wire_digest:
            code = "projection_changed"
            raise ArchiveRefused(code)
        self._wire_digest = wire_digest
        return wire

    async def _outbound(self, messages: list[ModelMessage]) -> list[ModelMessage]:
        wire = await self._wire()
        archive_request = ModelRequest(parts=[UserPromptPart(wire, timestamp=self._timestamp)])
        return [archive_request, *deepcopy(messages)]

    def prepare_messages(
        self,
        messages: list[ModelMessage],
        model_request_parameters: ModelRequestParameters | None = None,
    ) -> list[ModelMessage]:
        """Protect caller-owned messages from the wrapped adapter's preparation."""
        return self.wrapped.prepare_messages(deepcopy(messages), model_request_parameters)

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        """Resolve and count the same bounded envelope that a request will send."""
        return await self.wrapped.count_tokens(await self._outbound(messages), model_settings, model_request_parameters)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Recheck the receipt immediately before the provider request."""
        return await self.wrapped.request(await self._outbound(messages), model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """Recheck before opening a stream, without changing stream ownership."""
        async with self.wrapped.request_stream(
            await self._outbound(messages), model_settings, model_request_parameters, run_context
        ) as response:
            yield response
