"""Enforce the granted context capacity on each Pydantic AI model request."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import UsageLimits

from lychd.domain.cortex.context import ContextBudgetExceededError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import RequestUsage


def limit_model_requests(
    model: Model | None,
    *,
    context_window: int | None,
    output_reserve: int,
    counter_model: Model | None = None,
) -> tuple[Model | None, UsageLimits | None]:
    """Bind request capacity while preserving the actual provider's counter support.

    A transient projection adapter supplies its underlying provider as
    ``counter_model`` so its forwarding method cannot invent token counting.
    """
    if context_window is None:
        return model, None
    if output_reserve >= context_window:
        msg = (
            f"Output reserve {output_reserve} leaves no input budget inside the {context_window}-token context window."
        )
        raise ContextBudgetExceededError(msg)
    counter = counter_model if counter_model is not None else model
    count_before_request = counter is not None and type(counter).count_tokens is not Model.count_tokens
    if isinstance(counter, OpenAIResponsesModel) and type(counter).count_tokens is OpenAIResponsesModel.count_tokens:
        count_before_request = counter.profile.get("openai_supports_input_token_counting", True)
    limits = UsageLimits(count_tokens_before_request=count_before_request)
    if model is None:
        return None, limits
    return ContextWindowModel(model, input_tokens_limit=context_window - output_reserve), limits


class ContextWindowModel(WrapperModel):
    """Check individual input usage without changing cumulative Run accounting.

    The pinned adapter's UsageLimits governs cumulative usage. The model boundary
    retains individual request usage, including streamed responses and optional
    pre-counts, before the agent can act on a response.
    """

    def __init__(self, wrapped: Model, *, input_tokens_limit: int) -> None:
        """Bind one lease's available input capacity to its admitted model."""
        super().__init__(wrapped)
        self.input_tokens_limit = input_tokens_limit

    def _check(self, usage: RequestUsage) -> None:
        if usage.input_tokens > self.input_tokens_limit:
            msg = (
                f"Exceeded the per-request input_tokens_limit of {self.input_tokens_limit} "
                f"(input_tokens={usage.input_tokens})"
            )
            raise UsageLimitExceeded(msg)

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        """Refuse an oversized pre-count before its model request is sent."""
        usage = await self.wrapped.count_tokens(messages, model_settings, model_request_parameters)
        self._check(usage)
        return usage

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Check a completed response before tool or output processing."""
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        self._check(response.usage)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """Retain stream ownership and check final usage before the next agent step."""
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response:
            yield response
            self._check(response.usage)
