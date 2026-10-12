"""Bridge counting admission against real Responses models and inert HTTP transport."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded, UserError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import RequestUsage

from lychd.agents.workflows.bridge_chat import _request_policy
from lychd.domain.animation.links import Link
from lychd.domain.animation.model_factory import openai_compatible_provider
from lychd.domain.animation.schemas import ModelSurface
from lychd.domain.animation.services.adapters.surfaces import OpenAICompatibleConnector
from tests.agents.fakes import FakeGrant

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings


@dataclass
class _ResponsesEndpoint:
    """Observe every request, with counting unavailable unless the test admits it."""

    reported_tokens: int = 7
    counted_tokens: int | None = None
    paths: list[str] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        assert request.url.host == "runtime.test"
        self.paths.append(request.url.path)
        if request.url.path == "/v1/responses/input_tokens":
            if self.counted_tokens is None:
                return httpx2.Response(404, json={"error": {"message": "Counting endpoint unavailable"}})
            return httpx2.Response(200, json={"input_tokens": self.counted_tokens})
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["max_output_tokens"] == 20
        item: dict[str, Any] = {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        response: dict[str, Any] = {
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "model": payload["model"],
            "status": "in_progress",
            "output": [],
        }
        completed: dict[str, Any] = {
            **response,
            "status": "completed",
            "output": [
                {
                    **item,
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": []},
                    ],
                }
            ],
            "usage": {
                "input_tokens": self.reported_tokens,
                "output_tokens": 1,
                "total_tokens": self.reported_tokens + 1,
            },
        }
        if not payload.get("stream"):
            return httpx2.Response(200, json=completed)
        events: list[dict[str, Any]] = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_test",
                "delta": "ok",
                "logprobs": [],
            },
            {"type": "response.completed", "response": completed},
        ]
        content = "".join(
            "data: " + json.dumps({**event, "sequence_number": i}) + "\n\n" for i, event in enumerate(events)
        )
        return httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> _ResponsesEndpoint:
    endpoint = _ResponsesEndpoint()
    monkeypatch.setattr(
        "lychd.domain.animation.model_factory._model_http_client",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint)),
    )
    return endpoint


async def _run(model: Model, *, streaming: bool = False) -> str:
    grant = FakeGrant(model=model)
    grant.spec.generation_profile = SimpleNamespace(max_context=100, max_tokens=20)
    bounded, limits = _request_policy(100, grant)
    agent = Agent(bounded)
    with override_allow_model_requests(True):  # noqa: FBT003 - third-party positional API
        async with model:
            if not streaming:
                result = await agent.run("hello", usage_limits=limits, model_settings={"max_tokens": 20})
                return result.output
            async with agent.run_stream_events(
                "hello",
                usage_limits=limits,
                model_settings={"max_tokens": 20},
            ) as events:
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        return event.result.output
    pytest.fail("Agent did not return a completed result")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reported_tokens", [80, 81])
async def test_local_responses_uses_reported_usage_without_count_endpoint(
    endpoint: _ResponsesEndpoint,
    *,
    streaming: bool,
    reported_tokens: int,
) -> None:
    endpoint.reported_tokens = reported_tokens
    model = OpenAICompatibleConnector(
        link=Link(up=True),
        base_url="http://runtime.test/v1",
        default_model_id="o3",
        default_surface=ModelSurface.RESPONSES,
    ).get_model()
    assert isinstance(model, OpenAIResponsesModel)
    try:
        if reported_tokens <= 80:
            assert await _run(model, streaming=streaming) == "ok"
        else:
            with pytest.raises(UsageLimitExceeded, match="per-request input_tokens_limit of 80"):
                await _run(model, streaming=streaming)
    finally:
        assert model.client.is_closed()
    assert endpoint.paths == ["/v1/responses"]


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_counting", [False, True, None])
async def test_responses_counter_honors_profile_and_preserves_provider_default(
    endpoint: _ResponsesEndpoint,
    *,
    supports_counting: bool | None,
) -> None:
    endpoint.counted_tokens = 81
    profile = (
        None
        if supports_counting is None
        else OpenAIModelProfile(
            openai_supports_input_token_counting=supports_counting,
        )
    )
    model = OpenAIResponsesModel(
        "test-model",
        provider=openai_compatible_provider(base_url="http://runtime.test/v1"),
        profile=profile,
    )
    try:
        if supports_counting is False:
            assert await _run(model) == "ok"
        else:
            with pytest.raises(UsageLimitExceeded, match="per-request input_tokens_limit of 80"):
                await _run(model)
    finally:
        assert model.client.is_closed()
    assert endpoint.paths == (["/v1/responses"] if supports_counting is False else ["/v1/responses/input_tokens"])


class _LocallyCountingResponsesModel(OpenAIResponsesModel):
    """A real custom counter does not require the provider's counting endpoint."""

    counted_tokens = 7
    counts = 0

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        _ = messages, model_settings, model_request_parameters
        self.counts += 1
        return RequestUsage(input_tokens=self.counted_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("counted_tokens", [80, 81])
async def test_custom_responses_counter_works_with_server_counting_disabled(
    endpoint: _ResponsesEndpoint,
    counted_tokens: int,
) -> None:
    model = _LocallyCountingResponsesModel(
        "test-model",
        provider=openai_compatible_provider(base_url="http://runtime.test/v1"),
        profile=OpenAIModelProfile(openai_supports_input_token_counting=False),
    )
    model.counted_tokens = counted_tokens
    try:
        if counted_tokens <= 80:
            assert await _run(model) == "ok"
        else:
            with pytest.raises(UsageLimitExceeded, match="per-request input_tokens_limit of 80"):
                await _run(model)
    finally:
        assert model.client.is_closed()
    assert model.counts == 1
    assert endpoint.paths == (["/v1/responses"] if counted_tokens <= 80 else [])


@pytest.mark.asyncio
async def test_local_profile_refuses_explicit_server_count_without_transport(endpoint: _ResponsesEndpoint) -> None:
    model = OpenAICompatibleConnector(
        link=Link(up=True),
        base_url="http://runtime.test/v1",
        default_model_id="o3",
        default_surface=ModelSurface.RESPONSES,
    ).get_model()
    assert isinstance(model, OpenAIResponsesModel)
    with override_allow_model_requests(True):  # noqa: FBT003 - third-party positional API
        async with model:
            with pytest.raises(UserError, match="Server-side token counting is not available"):
                await model.count_tokens([ModelRequest.user_text_prompt("hello")], None, ModelRequestParameters())
    assert model.client.is_closed()
    assert endpoint.paths == []
