"""Actual local authentication, Origin/CSRF and raw-body refusal at the API."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from litestar import Litestar
from litestar.config.csrf import CSRFConfig
from litestar.datastructures import State

from lychd.compositions.creation.schemas import CaseView, CreationRefused, canonical
from lychd.compositions.creation.services import LocalCreationPolicy
from lychd.domain.codex.local_access import LocalOperatorAccess
from lychd.domain.codex.middleware import sigil_auth_middleware
from lychd.interface.api.creation import CreationController
from lychd.interface.web.openapi import StrictPydanticSchemaPlugin, build_openapi_config
from tests.unit.compositions.test_creation import StageStore, stage_fixture
from tests.web.conftest import TEST_ACCESS_PASSWORD, TEST_AUTHORIZATION

CSRF_VALUE = "inert-creation-csrf-secret"


class HTTPStore(StageStore):
    async def read(self, case_id: UUID) -> CaseView:
        assert self.view is not None
        if self.view.case_id != case_id:
            code = "unknown_case"
            raise CreationRefused(code)
        return self.view


def api() -> tuple[Litestar, Any, LocalOperatorAccess]:
    store = HTTPStore()
    service, _common = stage_fixture(store)
    access = LocalOperatorAccess(access_password=TEST_ACCESS_PASSWORD)
    service.policy = LocalCreationPolicy(access)
    assert store.view is not None
    admission = {"admission_id": str(store.view.case_id), "run_id": str(uuid4())}
    store.view = store.view.model_copy(update={"admissions": (admission,)})
    services = SimpleNamespace(creation=service, run_engine=None, bridge_sessions=None)
    app = Litestar(
        route_handlers=[CreationController],
        state=State({"services": services}),
        middleware=[sigil_auth_middleware(operator_access=access)],
        csrf_config=CSRFConfig(secret=CSRF_VALUE, cookie_name="creation-csrf", header_name="x-creation-csrf"),
        plugins=[StrictPydanticSchemaPlugin()],
        openapi_config=build_openapi_config(title="Creation test", version="1", use_handler_docstrings=False),
    )
    return app, store, access


@pytest.mark.asyncio
async def test_creation_http_requires_credentials_origin_csrf_and_current_role() -> None:
    app, store, access = api()
    view = cast("CaseView", store.view)
    route = f"/api/v1/creation/investigations/{view.case_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cast("Any", app)), base_url="http://testserver.local"
    ) as client:
        assert (await client.get(route)).status_code == 401
        client.headers["authorization"] = TEST_AUTHORIZATION
        assert (await client.get(route)).status_code == 200
        body = canonical(view.request)
        assert (
            await client.post(
                "/api/v1/creation/investigations", content=body, headers={"content-type": "application/json"}
            )
        ).status_code == 403
        client.headers["x-creation-csrf"] = client.cookies["creation-csrf"]
        foreign = await client.post(
            "/api/v1/creation/investigations",
            content=body,
            headers={"content-type": "application/json", "origin": "https://foreign.invalid"},
        )
        assert foreign.status_code == 403
        replay = await client.post(
            "/api/v1/creation/investigations", content=body, headers={"content-type": "application/json"}
        )
        assert replay.status_code == 200, replay.text
        duplicated = body[:-1] + b',"question":"changed"}'
        refused = await client.post(
            "/api/v1/creation/investigations", content=duplicated, headers={"content-type": "application/json"}
        )
        assert refused.status_code == 409
        assert store.events == []
        access.revoke()
        assert (await client.get(route)).status_code == 401


def test_openapi_has_typed_submit_and_continuation_bodies() -> None:
    app, _store, _access = api()
    schema = app.openapi_schema.to_schema()
    submit = schema["paths"]["/api/v1/creation/investigations"]["post"]
    continuation = schema["paths"]["/api/v1/creation/investigations/{case_id}/continue"]["post"]
    assert "InvestigationRequest" in str(submit["requestBody"])
    assert "ContinueRequest" in str(continuation["requestBody"])
    assert schema["components"]["schemas"]["InvestigationRequest"]["additionalProperties"] is False
