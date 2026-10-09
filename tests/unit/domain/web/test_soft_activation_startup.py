"""A durable soft attempt blocks startup before queue connection or publication."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest

from lychd.config.settings.root import Settings
from lychd.domain.orchestration.actuator import RuntimePreconditionError
from lychd.domain.orchestration.containment import SoftActivationRecord
from lychd.interface.web.lifespan import altar_services_lifespan
from lychd.system import constants
from lychd.system.services.runtime_guard import FileSoftActivationGuard

if TYPE_CHECKING:
    from pathlib import Path

    from litestar import Litestar


@pytest.mark.asyncio
@pytest.mark.parametrize("actuator", ["systemd", "host-reactor"])
async def test_soft_attempt_fences_startup_before_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actuator: str
) -> None:
    directory = tmp_path / "runtime-guard"
    directory.mkdir(mode=0o700)
    FileSoftActivationGuard(directory).arm(
        SoftActivationRecord(
            target_animator="router",
            target_capability_key="router:chat:large",
            config_generation="sha256:" + "a" * 64,
        )
    )
    monkeypatch.setattr(constants, "PATH_RUNTIME_GUARD_DIR", directory)
    settings = Settings.model_validate({"orchestration": {"switching": {"actuator": actuator}}})
    monkeypatch.setattr("lychd.config.settings.root.get_settings", lambda: settings)
    monkeypatch.setattr("lychd.extensions.host.get_extensions", SimpleNamespace)
    collect, connect, build, publish = Mock(), AsyncMock(), Mock(), Mock()
    monkeypatch.setattr("lychd.interface.web.lifespan._collect_run_queues", collect)
    monkeypatch.setattr("lychd.interface.web.lifespan.connect_run_queues", connect)
    monkeypatch.setattr("lychd.interface.web.lifespan.build_altar_services", build)
    monkeypatch.setattr("lychd.domain.cortex.substrate.set_run_substrate", publish)
    monkeypatch.setattr("lychd.interface.web.lifespan._stop_in_process_workers", AsyncMock())
    app = cast("Litestar", SimpleNamespace(state=SimpleNamespace(runes=object())))

    async def attempt_start() -> None:
        async with altar_services_lifespan(app):
            pytest.fail("An unresolved activation published the Vessel.")

    async with asyncio.timeout(2):
        with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
            await attempt_start()

    collect.assert_not_called()
    connect.assert_not_awaited()
    build.assert_not_called()
    publish.assert_not_called()
    assert FileSoftActivationGuard(directory).pending() is not None
