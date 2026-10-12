"""Real model client routing and retransmission with an inert transport boundary."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import httpx2
import pytest
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from lychd.domain.animation.links import Link
from lychd.domain.animation.schemas import ModelSurface
from lychd.domain.animation.services.adapters.surfaces import OpenAICompatibleConnector


def _model(surface: ModelSurface) -> OpenAIChatModel | OpenAIResponsesModel:
    model = OpenAICompatibleConnector(
        link=Link(up=True),
        base_url="http://127.0.0.1:23117/v1",
        default_model_id="synthetic-model",
        default_surface=surface,
    ).get_model()
    assert isinstance(model, OpenAIChatModel | OpenAIResponsesModel)
    return model


def _answer(surface: ModelSurface) -> httpx2.Response:
    if surface is ModelSurface.RESPONSES:
        return httpx2.Response(
            200,
            json={
                "id": "resp_transport",
                "object": "response",
                "created_at": 1,
                "model": "synthetic-model",
                "status": "completed",
                "output": [],
            },
        )
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-transport",
            "object": "chat.completion",
            "created": 1,
            "model": "synthetic-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ModelSurface.CHAT, ModelSurface.RESPONSES])
async def test_model_ignores_ambient_proxies_after_client_reentry(
    monkeypatch: pytest.MonkeyPatch, surface: ModelSurface
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://undeclared-proxy.test:8888")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    pools: list[str] = []
    bodies: list[bytes] = []

    async def observe(transport: httpx2.AsyncHTTPTransport, request: httpx2.Request) -> httpx2.Response:
        pools.append(type(transport._pool).__name__)
        bodies.append(await request.aread())
        return _answer(surface)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", observe)
    # A positive control proves the environment would route even this loopback URL through the proxy.
    async with httpx2.AsyncClient() as ambient:
        await ambient.post("http://127.0.0.1:23117/v1/control", content=b"control")
    assert pools.pop() == "AsyncHTTPProxy"
    bodies.clear()

    model = _model(surface)
    monkeypatch.setattr(model.client, "_platform", "Linux")
    for _ in range(2):
        with override_allow_model_requests(True):  # noqa: FBT003 - third-party positional API
            async with model:
                await model.request(
                    [ModelRequest.user_text_prompt("synthetic-private-input")], None, ModelRequestParameters()
                )
        assert model.client.is_closed()
    assert pools == ["AsyncConnectionPool", "AsyncConnectionPool"]
    assert all(b"synthetic-private-input" in body for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ModelSurface.CHAT, ModelSurface.RESPONSES])
@pytest.mark.parametrize("failure", ["server", "disconnect", "redirect"])
async def test_model_failure_does_not_retransmit_or_follow_redirect(
    monkeypatch: pytest.MonkeyPatch, surface: ModelSurface, failure: str
) -> None:
    requests: list[httpx2.Request] = []

    async def fail(_transport: httpx2.AsyncHTTPTransport, request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if failure == "disconnect":
            message = "synthetic lost acknowledgement"
            raise httpx2.ReadError(message, request=request)
        if failure == "redirect":
            return httpx2.Response(307, headers={"Location": "https://undeclared-provider.test/v1/other"})
        return httpx2.Response(500, json={"error": {"message": "synthetic failure"}})

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", fail)
    model = _model(surface)
    monkeypatch.setattr(model.client, "_platform", "Linux")
    with override_allow_model_requests(True):  # noqa: FBT003 - third-party positional API
        async with model:
            with pytest.raises(ModelAPIError):
                await model.request([ModelRequest.user_text_prompt("synthetic-input")], None, ModelRequestParameters())
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert model.client.is_closed()
