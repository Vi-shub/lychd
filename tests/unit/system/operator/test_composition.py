from __future__ import annotations

# pyright: reportPrivateUsage=false
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest

from lychd.system.operator import composition
from lychd.system.operator.inventory import OperatorPaths
from lychd.system.operator.models import ObservationState, OperatorAction, OperatorAuthorityError
from tests.unit.system.operator.test_control import (
    _catalog,
    _Inventory,
    _Runner,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_composition_keeps_active_stop_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _Inventory(_catalog(ObservationState.ACTIVE))
    runner = _Runner()
    paths = OperatorPaths(
        *(
            tmp_path / name
            for name in ("codex", "config", "receipt", "runes", "quadlet", "units", "storage", "snapshots")
        )
    )

    def trusted_tool(name: str) -> str:
        return f"/trusted/{name}"

    def inventory(**_kwargs: object) -> _Inventory:
        return world

    monkeypatch.setattr(composition, "trusted_host_tool", trusted_tool)
    monkeypatch.setattr(composition, "OperatorInventoryService", inventory)
    monkeypatch.setattr("lychd.system.services.lifecycle.lock.LifecycleLock", nullcontext)

    services = composition.build_operator_services(runner=runner, paths=paths)
    with pytest.raises(OperatorAuthorityError, match="authenticated lifecycle API completion is unavailable"):
        services.control.execute(OperatorAction.STOP)
    assert runner.calls == []
