"""Production-path safety checks for exact binding cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from lychd.system.operator import OwnedUnitCatalog, ProcessResult
from lychd.system.operator.inventory import OperatorInventoryService
from lychd.system.operator.process import ProcessInvocationError, ProcessRunner
from lychd.system.operator.retirement import OwnedUnitRetirementService
from lychd.system.operator.units import OwnedUnitInventoryService
from lychd.system.schemas import QuadletContainer
from lychd.system.services.lifecycle.bindings import BindingLifecycleService
from lychd.system.services.lifecycle.models import LifecycleError, LifecyclePlan
from lychd.system.services.scribe import OwnedBindings, ScribeService


@dataclass
class _SystemctlRunner:
    active_returncodes: list[int] = field(default_factory=list)
    enabled_returncodes: list[int] = field(default_factory=list)
    reload_returncodes: list[int] = field(default_factory=list)
    show_results: list[tuple[int, str] | ProcessInvocationError] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)

    def run(self, argv: tuple[str, ...], *, timeout_s: float) -> ProcessResult:
        self.calls.append((argv, timeout_s))
        if "daemon-reload" in argv:
            returncode = self.reload_returncodes.pop(0)
            stderr = "Failed to connect to bus" if returncode else ""
            stdout = ""
        elif "is-active" in argv:
            returncode = self.active_returncodes.pop(0)
            stderr = ""
            stdout = ""
        elif "is-enabled" in argv:
            returncode = self.enabled_returncodes.pop(0)
            stderr = ""
            stdout = ""
        elif "show" in argv:
            result = self.show_results.pop(0)
            if isinstance(result, ProcessInvocationError):
                raise result
            returncode, stdout = result
            stderr = ""
        else:  # pragma: no cover - catches unexpected production argv
            message = f"Unexpected systemctl invocation: {argv}"
            raise AssertionError(message)
        return ProcessResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )


def _bound_service(
    tmp_path: Path,
    runner: ProcessRunner,
    *,
    generated: bool = False,
) -> tuple[ScribeService, BindingLifecycleService, Path]:
    quadlet_dir = tmp_path / "containers" / "systemd"
    systemd_dir = tmp_path / "systemd" / "user"
    quadlet_dir.mkdir(parents=True)
    systemd_dir.mkdir(parents=True)
    scribe = ScribeService(
        output_dir=quadlet_dir,
        systemd_dir=systemd_dir,
    )
    if generated:
        scribe.reconcile_all(
            [
                QuadletContainer(
                    container_name="lychd-vessel", image="localhost/lychd-vessel:fixture", description="fixture"
                )
            ],
            plain_units={},
        )
        source = quadlet_dir / "lychd-vessel.container"
    else:
        scribe.reconcile_all(
            [],
            plain_units={"lychd-vessel.service": "[Service]\nExecStart=/usr/bin/true\n"},
        )
        source = systemd_dir / "lychd-vessel.service"
    lifecycle = BindingLifecycleService(
        scribe,
        runner=runner,
        systemctl_bin="/usr/bin/systemctl",
    )
    return scribe, lifecycle, source


@dataclass
class _RetirementRunner:
    unit_file_state: str
    active: bool = True
    reloaded: bool = False
    source: Path | None = None
    authority: Path | None = None
    calls: list[tuple[tuple[str, ...], float]] = field(default_factory=list)

    def run(self, argv: tuple[str, ...], *, timeout_s: float) -> ProcessResult:
        self.calls.append((argv, timeout_s))
        verb = argv[2]
        assert self.source is not None
        assert self.authority is not None
        assert self.authority.exists()
        if verb == "show":
            if self.reloaded:
                stdout = "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
            else:
                active = "active" if self.active else "inactive"
                stdout = f"LoadState=loaded\nActiveState={active}\nUnitFileState={self.unit_file_state}\n"
            return ProcessResult(argv, 0, stdout)
        if verb == "stop":
            assert argv[3:] == ("lychd-vessel.service",)
            assert self.source.exists()
            self.active = False
            return ProcessResult(argv, 0)
        if verb == "is-active":
            return ProcessResult(argv, 0 if self.active else 3)
        if verb == "is-enabled":
            # systemd v258 documents exit 0 for both generated and static.
            assert self.unit_file_state in {"generated", "static"}
            return ProcessResult(argv, 0, self.unit_file_state + "\n")
        if verb == "daemon-reload":
            assert not self.source.exists()
            assert not self.reloaded
            self.reloaded = True
            return ProcessResult(argv, 0)
        message = f"Unexpected systemctl invocation: {argv}"
        raise AssertionError(message)


@dataclass
class _RetirementInventory:
    units: OwnedUnitInventoryService

    def owned_units(self) -> OwnedUnitCatalog:
        return self.units.inspect()


def _unit_plan(tmp_path: Path, runner: ProcessRunner, *, post_reload: bool = False) -> LifecyclePlan:
    """Exercise the state guard independently of Linux-only Scribe mutation."""
    service = BindingLifecycleService(
        ScribeService(output_dir=tmp_path / "quadlet", systemd_dir=tmp_path / "systemd"),
        runner=runner,
        systemctl_bin="/usr/bin/systemctl",
    )
    owned = OwnedBindings(receipt_present=True, generation="fixture", runtime_units=("lychd-vessel.service",))
    actions = service._post_reload_unit_actions(owned) if post_reload else service._unit_actions(owned)  # pyright: ignore[reportPrivateUsage]
    return LifecyclePlan(actions=tuple(actions))


@pytest.mark.parametrize("unit_file_state", ["generated", "static"])
def test_inactive_generated_and_static_state_allows_source_removal(
    tmp_path: Path,
    unit_file_state: str,
) -> None:
    runner = _SystemctlRunner(
        active_returncodes=[3],
        enabled_returncodes=[0],
        show_results=[(0, f"LoadState=loaded\nActiveState=inactive\nUnitFileState={unit_file_state}\n")],
    )

    _unit_plan(tmp_path, runner).require_executable()


@pytest.mark.parametrize(
    "properties",
    [
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n",
        "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n",
    ],
    ids=["disabled", "absent"],
)
def test_inactive_disabled_or_absent_state_allows_source_removal(tmp_path: Path, properties: str) -> None:
    _unit_plan(tmp_path, _SystemctlRunner(show_results=[(0, properties)])).require_executable()


@pytest.mark.parametrize(
    ("load_state", "active_state", "unit_file_state"),
    [
        ("loaded", "active", "generated"),
        ("loaded", "activating", "generated"),
        ("loaded", "deactivating", "static"),
        ("loaded", "reloading", "disabled"),
        ("loaded", "failed", "disabled"),
        ("loaded", "unknown", "disabled"),
        ("loaded", "inactive", "enabled"),
        ("loaded", "inactive", "enabled-runtime"),
        ("loaded", "inactive", "linked"),
        ("loaded", "inactive", "linked-runtime"),
        ("loaded", "inactive", "masked"),
        ("masked", "inactive", "masked-runtime"),
        ("loaded", "inactive", "indirect"),
        ("loaded", "inactive", "unknown"),
        ("loaded", "inactive", ""),
        ("error", "inactive", "disabled"),
        ("not-found", "active", ""),
        ("not-found", "inactive", "generated"),
        ("not-found", "inactive", "enabled"),
    ],
)
def test_unproven_inert_state_blocks_source_removal(
    tmp_path: Path,
    load_state: str,
    active_state: str,
    unit_file_state: str,
) -> None:
    properties = f"LoadState={load_state}\nActiveState={active_state}\nUnitFileState={unit_file_state}\n"
    runner = _SystemctlRunner(show_results=[(0, properties)])

    with pytest.raises(LifecycleError, match="not provably inactive"):
        _unit_plan(tmp_path, runner).require_executable()

    assert [argv[2] for argv, _ in runner.calls] == ["show"]


@pytest.mark.parametrize(
    "properties",
    [
        "",
        "LoadState=loaded\nActiveState=inactive\n",
        "LoadState=loaded\nUnitFileState=static\n",
        "ActiveState=inactive\nUnitFileState=static\n",
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=static\nUnitFileState=enabled\n",
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=static\nmalformed\n",
        "LoadState=loaded\nActiveState=inactive\nUnexpected=static\n",
    ],
    ids=["empty", "missing-file-state", "missing-activity", "missing-load", "duplicate", "malformed", "unexpected"],
)
def test_malformed_systemd_properties_block_source_removal(tmp_path: Path, properties: str) -> None:
    runner = _SystemctlRunner(show_results=[(0, properties)])

    with pytest.raises(LifecycleError, match="missing or malformed"):
        _unit_plan(tmp_path, runner).require_executable()


@pytest.mark.parametrize("returncode", [1, 4])
def test_failed_systemd_probe_blocks_source_removal(tmp_path: Path, returncode: int) -> None:
    runner = _SystemctlRunner(
        show_results=[(returncode, "LoadState=loaded\nActiveState=inactive\nUnitFileState=static\n")],
    )

    with pytest.raises(LifecycleError, match="state check failed"):
        _unit_plan(tmp_path, runner).require_executable()


def test_unexecutable_systemd_probe_blocks_source_removal(tmp_path: Path) -> None:
    runner = _SystemctlRunner(show_results=[ProcessInvocationError("probe timed out")])

    with pytest.raises(LifecycleError, match="could not be executed"):
        _unit_plan(tmp_path, runner).require_executable()


@pytest.mark.parametrize("unit_file_state", ["generated", "static"])
def test_post_reload_generated_or_static_state_blocks_authority_release(tmp_path: Path, unit_file_state: str) -> None:
    runner = _SystemctlRunner(
        show_results=[(0, f"LoadState=loaded\nActiveState=inactive\nUnitFileState={unit_file_state}\n")],
    )

    with pytest.raises(LifecycleError, match="not stably inactive"):
        _unit_plan(tmp_path, runner, post_reload=True).require_executable()


@pytest.mark.parametrize("unit_file_state", ["generated", "static"])
def test_retired_generated_and_static_bindings_remove_without_disable(
    tmp_path: Path,
    unit_file_state: str,
) -> None:
    runner = _RetirementRunner(unit_file_state)
    scribe, lifecycle, source = _bound_service(tmp_path, runner, generated=unit_file_state == "generated")
    runner.source = source
    runner.authority = scribe.ownership_path
    inventory = _RetirementInventory(OwnedUnitInventoryService(scribe, runner, systemctl_bin="/usr/bin/systemctl"))
    retirement = OwnedUnitRetirementService(
        inventory=cast("OperatorInventoryService", inventory),
        runner=runner,
        systemctl_bin="/usr/bin/systemctl",
    )

    plan = retirement.plan()
    assert plan.owned_units == ("lychd-vessel.service",)
    assert plan.stop_units == plan.owned_units
    assert plan.disable_units == ()
    assert source.exists()
    retirement.execute(plan)
    assert not runner.active
    lifecycle.plan_destroy().require_executable()
    lifecycle.destroy()

    assert not source.exists()
    assert not scribe.ownership_path.exists()
    assert [argv[2] for argv, _ in runner.calls] == [
        "show",
        "show",
        "stop",
        "show",
        "show",
        "show",
        "daemon-reload",
        "show",
    ]
    assert all(timeout_s == (30.0 if argv[2] in {"stop", "daemon-reload"} else 3.0) for argv, timeout_s in runner.calls)


def test_reload_failure_retains_exact_authority_for_idempotent_retry(
    tmp_path: Path,
) -> None:
    runner = _SystemctlRunner(
        active_returncodes=[3, 3, 3, 3],
        enabled_returncodes=[1, 1, 1, 1],
        reload_returncodes=[1, 0],
        show_results=[
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (
                0,
                "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n",
            ),
        ],
    )
    scribe, lifecycle, source = _bound_service(tmp_path, runner)
    lifecycle.plan_destroy().require_executable()

    with pytest.raises(LifecycleError, match="daemon-reload failed"):
        lifecycle.destroy()

    retained = scribe.inspect_owned_bindings()
    assert retained.receipt_present
    assert retained.systemd_sources == (source,)
    assert retained.runtime_units == ("lychd-vessel.service",)
    assert not source.exists()
    assert scribe.ownership_path.exists()

    lifecycle.plan_destroy().require_executable()
    lifecycle.destroy()

    assert not scribe.ownership_path.exists()
    assert not source.exists()
    assert all(timeout_s in {3.0, 30.0} for _, timeout_s in runner.calls)


@pytest.mark.parametrize(
    "post_reload_properties",
    [
        "LoadState=loaded\nActiveState=active\nUnitFileState=disabled\n",
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=generated\n",
        "LoadState=loaded\nActiveState=inactive\nUnitFileState=static\n",
    ],
    ids=["restarted", "still-generated", "still-static"],
)
def test_unit_state_after_reload_blocks_authority_release(
    tmp_path: Path,
    post_reload_properties: str,
) -> None:
    runner = _SystemctlRunner(
        active_returncodes=[3, 3],
        enabled_returncodes=[1, 1],
        reload_returncodes=[0],
        show_results=[
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (0, "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"),
            (0, post_reload_properties),
        ],
    )
    scribe, lifecycle, source = _bound_service(tmp_path, runner)
    lifecycle.plan_destroy().require_executable()

    with pytest.raises(LifecycleError, match="not stably inactive"):
        lifecycle.destroy()

    retained = scribe.inspect_owned_bindings()
    assert retained.receipt_present
    assert retained.systemd_sources == (source,)
    assert retained.runtime_units == ("lychd-vessel.service",)
    assert not source.exists()
    assert scribe.ownership_path.exists()
