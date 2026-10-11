"""Real factory HTTP assembly, without entering host/database/worker lifespan.

The production routes, providers, plugins and security configuration remain intact.
In-process requests use memory services; this is not startup or recovery evidence.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from lychd import app as app_module
from lychd.config import components
from lychd.config.runes import registry as rune_registry_module
from lychd.config.runes.registry import RuneRegistry
from lychd.config.settings.root import SettingsSnapshot
from lychd.config.settings.server import DatabaseSettings
from lychd.db.factory import create_db_engine
from lychd.extensions import host as extension_host
from lychd.extensions.host import AssembledExtensions
from lychd.extensions.manager import ExtensionManager
from lychd.interface.web.altar_services import build_altar_services
from tests.runtime_guards import InertSoftActivationGuard
from tests.web.conftest import TEST_ACCESS_PASSWORD, TEST_AUTHORIZATION

_ORIGIN = "http://127.0.0.1:5173"
_PROJECTS = "/api/v1/atlas/projects"


class _UnusedQueue:
    """Fail if an HTTP assembly test accidentally attempts execution."""

    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> None:
        _ = job_or_func, kwargs
        pytest.fail("HTTP assembly must not enqueue work")

    async def job(self, job_key: str, /) -> None:
        _ = job_key
        pytest.fail("HTTP assembly must not inspect queue jobs")

    async def abort(self, job: Any, error: str, /, ttl: float = 5) -> None:
        _ = job, error, ttl
        pytest.fail("HTTP assembly must not abort queue jobs")


@pytest_asyncio.fixture
async def factory_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Assemble the real app with isolated configuration, without running lifespan."""
    settings = SettingsSnapshot(
        payload='{"server":{"database":{"profile":"memory"},"web":{"allowed_cors_origins":["http://127.0.0.1:5173"]}}}',
        database_runtime_password=SecretStr("synthetic-database-password"),
        web_secret_key=SecretStr("synthetic-factory-signing-key"),
        web_access_password=SecretStr(TEST_ACCESS_PASSWORD),
    ).materialize()
    settings.orchestration.switching.host_reactor_dir = tmp_path / "reactor" / "inbox"
    extensions = AssembledExtensions(context=ExtensionManager(builtins=(), crypt=()).assemble())
    runes = RuneRegistry(())

    def empty_runes(_extensions: AssembledExtensions) -> RuneRegistry:
        return runes

    # Exclude ambient launch arguments and configuration without bypassing policy.
    for variable in tuple(os.environ):
        if variable.startswith(("LITESTAR_", "GRANIAN_")) or variable == "WEB_CONCURRENCY":
            monkeypatch.delenv(variable)
    monkeypatch.setattr(sys, "argv", ["python"])
    monkeypatch.setattr(sys, "orig_argv", ["python"])
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(extension_host, "get_extensions", lambda: extensions)
    monkeypatch.setattr(rune_registry_module, "load_rune_registry", empty_runes)

    # Engine construction opens no connection. Keep its pool outside the process memo.
    engine = create_db_engine(settings.server.database, runtime=True)

    def isolated_engine(_settings: DatabaseSettings) -> AsyncEngine:
        return engine

    monkeypatch.setattr(components, "get_engine", isolated_engine)
    services = build_altar_services(
        queues={"runs": _UnusedQueue(), "rites": _UnusedQueue()},
        runes=runes,
        runtime_adapters=(),
        soft_activation_guard=InertSoftActivationGuard(),
        settings=settings,
    )
    try:
        app = app_module.create_app()
        app.state.services = services
        # ASGITransport dispatches HTTP only: no lifecycle, listener or worker starts.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),  # pyright: ignore[reportArgumentType] - ASGI typing differs
            base_url="http://127.0.0.1:7134",
        ) as client:
            yield client
    finally:
        await services.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_factory_installs_host_and_possession_admission(factory_client: httpx.AsyncClient) -> None:
    foreign = await factory_client.get(
        _PROJECTS, headers={"host": "attacker.example", "authorization": TEST_AUTHORIZATION}
    )
    assert foreign.status_code == 400
    assert foreign.json() == {"message": "invalid host header"}

    missing = await factory_client.get(_PROJECTS)
    wrong = await factory_client.get(_PROJECTS, auth=("magus", "wrong-credential"))
    assert missing.status_code == wrong.status_code == 401
    assert missing.headers["www-authenticate"].startswith("Basic ")
    admitted = await factory_client.get(_PROJECTS, headers={"authorization": TEST_AUTHORIZATION})
    assert admitted.status_code == 200


@pytest.mark.asyncio
async def test_factory_installs_exact_cors_origin(factory_client: httpx.AsyncClient) -> None:
    headers = {"authorization": TEST_AUTHORIZATION}
    admitted = await factory_client.get(_PROJECTS, headers={**headers, "origin": _ORIGIN})
    foreign = await factory_client.get(_PROJECTS, headers={**headers, "origin": "http://localhost:5173"})
    assert admitted.status_code == foreign.status_code == 200
    assert admitted.headers["access-control-allow-origin"] == _ORIGIN
    assert "access-control-allow-origin" not in foreign.headers


@pytest.mark.asyncio
async def test_factory_requires_csrf_before_atlas_mutation(factory_client: httpx.AsyncClient) -> None:
    factory_client.headers["authorization"] = TEST_AUTHORIZATION
    body = {"id": str(uuid4()), "title": "Factory security boundary"}
    missing = await factory_client.post(_PROJECTS, json=body)
    assert missing.status_code == 403

    initial = await factory_client.get(_PROJECTS)
    assert initial.status_code == 200
    token = factory_client.cookies["csrftoken"]
    headers = {"x-csrftoken": token}
    foreign = await factory_client.post(_PROJECTS, json=body, headers={**headers, "origin": "https://attacker.example"})
    assert foreign.status_code == 403
    assert (await factory_client.get(f"{_PROJECTS}/{body['id']}")).status_code == 404

    admitted = await factory_client.post(_PROJECTS, json=body, headers=headers)
    assert admitted.status_code == 201
    assert admitted.json()["id"] == body["id"]
    assert (await factory_client.get(f"{_PROJECTS}/{body['id']}")).status_code == 200
