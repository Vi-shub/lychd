"""Canvas controller admission and real OpenAPI schema exposure, without services."""

from __future__ import annotations

import base64
from typing import Any, cast
from uuid import uuid4

import pytest
from litestar import Litestar
from litestar.config.csrf import CSRFConfig

from lychd.domain.codex.middleware import sigil_auth_middleware
from lychd.domain.codex.sigil import Sigil
from lychd.interface.web.canvas import CanvasController
from lychd.interface.web.openapi import StrictPydanticSchemaPlugin, build_openapi_config
from tests.web.conftest import AsgiClient

_PASSWORD = "canvas-synthetic-test-password-0123456789"  # noqa: S105
_AUTHORIZATION = "Basic " + base64.b64encode(f"magus:{_PASSWORD}".encode()).decode("ascii")


@pytest.fixture
def canvas_client() -> AsgiClient:
    app = Litestar(
        route_handlers=[CanvasController],
        middleware=[sigil_auth_middleware(access_password=_PASSWORD)],
        csrf_config=CSRFConfig(secret=str(uuid4()), cookie_secure=False),
        plugins=[StrictPydanticSchemaPlugin()],
        openapi_config=build_openapi_config(
            title="Canvas isolated contract", version="test", use_handler_docstrings=True
        ),
    )
    client = AsgiClient(app, headers={"authorization": _AUTHORIZATION})
    token = client.get("/api/v1/canvas/contracts").cookies["csrftoken"]
    client.headers.update({"cookie": f"csrftoken={token}", "x-csrftoken": token})
    return client


def _request() -> dict[str, Any]:
    scene_id, node_id = str(uuid4()), str(uuid4())
    node: dict[str, Any] = {
        "id": node_id,
        "x": 0,
        "y": 0,
        "width": 120,
        "height": 60,
        "label": "Original",
        "kind": "box",
        "pinned": False,
        "source_stroke_ids": [],
        "artwork": None,
    }
    return {
        "packet": {
            "format": "lychd.canvas.selection/1",
            "scene": {
                "format": "lychd.canvas/1",
                "id": scene_id,
                "revision": 1,
                "title": "Example",
                "strokes": [],
                "nodes": [node],
                "edges": [],
            },
            "selection": {
                "scene_id": scene_id,
                "revision": 1,
                "object_ids": [node_id],
                "bounds": {"x": 0, "y": 0, "width": 120, "height": 60},
            },
            "instruction": "Rename this node.",
            "crop": None,
        },
        "proposal": {
            "format": "lychd.canvas.proposal/1",
            "id": str(uuid4()),
            "scene_id": scene_id,
            "base_revision": 1,
            "selection_ids": [node_id],
            "author": "Explicit test fixture",
            "summary": "Rename the selected node.",
            "nodes": [{**node, "label": "Reviewed"}],
            "edges": [],
            "archive_stroke_ids": [],
        },
    }


def test_canvas_requires_authentication(canvas_client: AsgiClient) -> None:
    response = canvas_client.get("/api/v1/canvas/contracts", headers={"authorization": ""})
    assert response.status_code == 401


def test_canvas_descriptor_disclaims_retention_and_provider_execution(canvas_client: AsgiClient) -> None:
    response = canvas_client.get("/api/v1/canvas/contracts")
    assert response.status_code == 200
    body = response.json()
    assert body["composition"] == "canvas.board"
    assert body["server_retention"] is False
    assert body["model_transmission"] is False
    assert body["image_generation"] is False


def test_review_returns_one_successor_and_retains_no_state(canvas_client: AsgiClient) -> None:
    request = _request()
    first = canvas_client.post("/api/v1/canvas/review", json=request)
    second = canvas_client.post("/api/v1/canvas/review", json=request)
    assert first.status_code == 200
    assert first.json() == second.json()
    assert first.json()["scene"]["revision"] == 2
    assert first.json()["scene"]["nodes"][0]["label"] == "Reviewed"
    assert request["packet"]["scene"]["nodes"][0]["label"] == "Original"


def test_review_refuses_stale_and_outside_selection_updates(canvas_client: AsgiClient) -> None:
    request = _request()
    request["proposal"]["base_revision"] = 2
    assert canvas_client.post("/api/v1/canvas/review", json=request).status_code == 400
    request = _request()
    existing = {**request["packet"]["scene"]["nodes"][0], "id": str(uuid4()), "label": "Context"}
    request["packet"]["scene"]["nodes"].append(existing)
    request["proposal"]["nodes"] = [{**existing, "label": "Unauthorized"}]
    assert canvas_client.post("/api/v1/canvas/review", json=request).status_code == 400


def test_document_identity_validation_and_malformed_import(canvas_client: AsgiClient) -> None:
    document: dict[str, Any] = {
        "format": "lychd.canvas.document/1",
        "scene": _request()["packet"]["scene"],
        "past": [],
        "future": [],
    }
    response = canvas_client.post("/api/v1/canvas/documents/validate", json=document)
    assert response.status_code == 200
    assert response.json() == document
    document["unexpected"] = True
    assert canvas_client.post("/api/v1/canvas/documents/validate", json=document).status_code == 400


def test_canvas_write_scope_is_required(canvas_client: AsgiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "lychd.domain.codex.middleware.default_local_sigil",
        lambda: Sigil(name="synthetic-reader", scopes=frozenset({"altar:read"})),
    )
    assert canvas_client.get("/api/v1/canvas/contracts").status_code == 200
    assert canvas_client.post("/api/v1/canvas/review", json=_request()).status_code == 403


def test_canvas_mutation_requires_csrf_in_addition_to_authenticated_scope(canvas_client: AsgiClient) -> None:
    unprotected = AsgiClient(canvas_client.app, headers={"authorization": _AUTHORIZATION})
    assert unprotected.post("/api/v1/canvas/review", json=_request()).status_code == 403
    assert canvas_client.post("/api/v1/canvas/review", json=_request()).status_code == 200
    assert (
        canvas_client.post(
            "/api/v1/canvas/review", json=_request(), headers={"origin": "https://example.invalid"}
        ).status_code
        == 403
    )


def test_canvas_schemas_are_exposed_by_real_controller_signatures(canvas_client: AsgiClient) -> None:
    response = canvas_client.get("/schema/openapi.json")
    assert response.status_code == 200
    schema = cast("dict[str, Any]", response.json())
    models = schema["components"]["schemas"]
    expected = {
        "CanvasPoint",
        "CanvasBounds",
        "CanvasStroke",
        "CanvasArtwork",
        "CanvasNode",
        "CanvasEdge",
        "CanvasScene",
        "CanvasDocument",
        "CanvasSelection",
        "CanvasCrop",
        "CanvasPacket",
        "CanvasProposal",
        "CanvasReviewRequest",
        "CanvasReviewResult",
    }
    assert expected <= models.keys()
    assert all(models[name]["additionalProperties"] is False for name in expected)
    assert "/api/v1/canvas/review" in schema["paths"]
    assert "/api/v1/canvas/documents/validate" in schema["paths"]
