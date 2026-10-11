from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from lychd.system.operator import (
    ObservationState,
    OwnedUnit,
    OwnedUnitInventoryService,
    ProcessResult,
)
from lychd.system.services.scribe import OwnedBindings

if TYPE_CHECKING:
    from lychd.system.services.scribe import ScribeService


class _Scribe:
    def __init__(self, bindings: OwnedBindings | Exception) -> None:
        self.bindings = bindings

    def inspect_owned_bindings(self) -> OwnedBindings:
        if isinstance(self.bindings, Exception):
            raise self.bindings
        return self.bindings


class _Runner:
    def __init__(self, results: list[ProcessResult]) -> None:
        self.results = results
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv: tuple[str, ...], *, timeout_s: float) -> ProcessResult:
        self.calls.append((argv, timeout_s))
        return self.results.pop(0)


def test_unit_inventory_observes_only_receipted_runtime_units(tmp_path: Path) -> None:
    source = tmp_path / "lychd-vessel.container"
    bindings = OwnedBindings(
        receipt_present=True,
        generation="generation",
        quadlet_sources=(source,),
        runtime_units=("lychd-vessel.service",),
    )
    runner = _Runner(
        [
            ProcessResult(
                argv=(),
                returncode=0,
                stdout="LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
            )
        ]
    )
    service = OwnedUnitInventoryService(
        cast("ScribeService", _Scribe(bindings)),
        runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    catalog = service.inspect()

    assert catalog.generation == "generation"
    assert catalog.units[0].state is ObservationState.ACTIVE
    assert catalog.units[0].sources == (str(source),)
    assert runner.calls == [
        (
            (
                "/usr/bin/systemctl",
                "--user",
                "show",
                "lychd-vessel.service",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=UnitFileState",
            ),
            3.0,
        )
    ]


def test_absent_scribe_receipt_does_not_scan_systemd() -> None:
    runner = _Runner([])
    service = OwnedUnitInventoryService(
        cast("ScribeService", _Scribe(OwnedBindings(receipt_present=False))),
        runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    assert service.inspect().units == ()
    assert runner.calls == []


def test_invalid_scribe_authority_degrades_without_scanning() -> None:
    runner = _Runner([])
    service = OwnedUnitInventoryService(
        cast("ScribeService", _Scribe(RuntimeError("corrupt receipt"))),
        runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    catalog = service.inspect()

    assert catalog.warning == "Cannot validate Scribe ownership: corrupt receipt"
    assert runner.calls == []


def test_deactivating_vessel_remains_active_for_authority_arbitration(
    tmp_path: Path,
) -> None:
    """Shutdown work must finish before direct lifecycle authority can return."""
    source = tmp_path / "lychd-vessel.container"
    runner = _Runner(
        [
            ProcessResult(
                argv=(),
                returncode=0,
                stdout=("LoadState=loaded\nActiveState=deactivating\nUnitFileState=enabled\n"),
            )
        ]
    )
    service = OwnedUnitInventoryService(
        cast(
            "ScribeService",
            _Scribe(
                OwnedBindings(
                    receipt_present=True,
                    generation="generation",
                    quadlet_sources=(source,),
                    runtime_units=("lychd-vessel.service",),
                )
            ),
        ),
        runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    assert service.inspect().units[0].state is ObservationState.ACTIVE


@pytest.mark.parametrize("load_state", ["loaded", "not-found", "bad-setting"])
@pytest.mark.parametrize(
    ("active_state", "state"),
    [
        ("active", ObservationState.ACTIVE),
        ("activating", ObservationState.ACTIVE),
        ("reloading", ObservationState.ACTIVE),
        ("deactivating", ObservationState.ACTIVE),
        ("inactive", ObservationState.INACTIVE),
        ("failed", ObservationState.FAILED),
    ],
)
def test_unit_inventory_retains_load_state_without_changing_activity(
    load_state: str,
    active_state: str,
    state: ObservationState,
) -> None:
    runner = _Runner(
        [ProcessResult((), 0, f"LoadState={load_state}\nActiveState={active_state}\nUnitFileState=disabled\n")]
    )
    service = OwnedUnitInventoryService(
        cast("ScribeService", _Scribe(OwnedBindings(receipt_present=True, runtime_units=("lychd-test.service",)))),
        runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    unit = service.inspect().units[0]

    assert unit.state is state
    assert unit.unit_file_state == "disabled"
    assert unit.detail == ("" if load_state == "loaded" else f"load state: {load_state}")
    assert unit.load_state == load_state


def test_owned_unit_default_does_not_fabricate_loaded_or_shift_positional_detail() -> None:
    unit = OwnedUnit(
        "lychd-test.service", ("/units/lychd-test.service",), ObservationState.INACTIVE, "disabled", "observed"
    )

    assert unit.detail == "observed"
    assert unit.load_state == "unknown"
