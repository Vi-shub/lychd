"""One supported persistence profile must own every constructed application store."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from lychd.config.runes.registry import RuneRegistry
from lychd.config.settings.root import SettingsSnapshot
from lychd.interface.web import altar_services
from tests.runtime_guards import InertSoftActivationGuard


class UnusedQueue:
    """Application assembly must not issue work or inspect a queue."""

    async def enqueue(self, job_or_func: str, /, **kwargs: Any) -> None:
        _ = job_or_func, kwargs
        pytest.fail("Assembly must not enqueue work")

    async def job(self, job_key: str, /) -> None:
        _ = job_key
        pytest.fail("Assembly must not inspect jobs")

    async def abort(self, job: Any, error: str, /, ttl: float = 5) -> None:
        _ = job, error, ttl
        pytest.fail("Assembly must not abort work")


@pytest.mark.parametrize("profile", ["", "postgress", "POSTGRES", " memory", "postgres\n", "sqlite"])
def test_unsupported_profile_refuses_before_collaborator_construction(
    monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    settings = SettingsSnapshot(payload="{}").materialize()
    archive = Mock(side_effect=AssertionError("No collaborator may be constructed"))
    sessions = Mock(side_effect=AssertionError("No database factory may be accessed"))
    monkeypatch.setattr(altar_services, "_build_archive", archive)
    monkeypatch.setattr("lychd.db.engine.get_session_factory", sessions)

    with pytest.raises(ValueError, match="Unsupported persistence profile"):
        altar_services.build_altar_services(queues={}, runes=RuneRegistry(()), settings=settings, profile=profile)

    archive.assert_not_called()
    sessions.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "configured", "expected"),
    [
        ("memory", "postgres", "memory"),
        ("postgres", "memory", "postgres"),
        (None, "memory", "memory"),
        (None, "postgres", "postgres"),
    ],
)
async def test_supported_profile_selects_matching_stores(
    monkeypatch: pytest.MonkeyPatch, override: str | None, configured: str, expected: str
) -> None:
    from lychd.db.atlas import DbAtlasStore
    from lychd.db.checkpoints import PostgresStasisStore
    from lychd.db.sessions import DbBridgeSessionStore
    from lychd.domain.codex.ledger import CodexConsentLedger, InMemoryConsentLedger
    from lychd.domain.cortex.ledger import DbRunLedger, InMemoryRunLedger
    from lychd.domain.cortex.stasis import InMemoryStasisStore
    from lychd.domain.web.atlas import InMemoryAtlasStore
    from lychd.domain.web.sessions import BridgeSessionStore

    settings = SettingsSnapshot(payload=json.dumps({"server": {"database": {"profile": configured}}})).materialize()
    factory = Mock(name="inert_session_factory")
    get_factory = Mock(return_value=factory)
    monkeypatch.setattr("lychd.db.engine.get_session_factory", get_factory)
    services = altar_services.build_altar_services(
        queues={"runs": UnusedQueue(), "rites": UnusedQueue()},
        runes=RuneRegistry(()),
        settings=settings,
        profile=override,
        soft_activation_guard=InertSoftActivationGuard(),
    )
    try:
        if expected == "memory":
            assert isinstance(services.ledger, InMemoryRunLedger)
            assert isinstance(services.consents, InMemoryConsentLedger)
            assert isinstance(services.bridge_sessions, BridgeSessionStore)
            assert isinstance(services.atlas, InMemoryAtlasStore)
            assert isinstance(services.substrate.stasis_store, InMemoryStasisStore)
            get_factory.assert_not_called()
        else:
            assert isinstance(services.ledger, DbRunLedger)
            assert isinstance(services.consents, CodexConsentLedger)
            assert isinstance(services.bridge_sessions, DbBridgeSessionStore)
            assert isinstance(services.atlas, DbAtlasStore)
            assert isinstance(services.substrate.stasis_store, PostgresStasisStore)
            get_factory.assert_called()
        factory.assert_not_called()
    finally:
        await services.aclose()
