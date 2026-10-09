from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock

import pytest

from lychd.domain.animation.protocols import CapabilityRegistry
from lychd.system.operator.process import ProcessResult
from lychd.system.services.runtime_recovery import SoftActivationRecovery
from lychd.system.services.scribe import OwnedBindings

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture

pytestmark = pytest.mark.asyncio

_ID = "a" * 32
_GENERATION = "sha256:" + "b" * 64
_VESSEL = "lychd-vessel.service"
_OTHER_VESSEL = "lychd-uncaged-vessel.service"
_SERVICE = "lychd-alpha.service"
_TARGET = "lychd-animator-alpha.target"


@dataclass
class _World:
    uncaged: bool = False
    lock_held: bool = False
    jobs: str = ""
    later_jobs: str | None = None
    job_checks: int = 0
    unit: str = _SERVICE
    property_name: str = "ActiveState"
    property_value: str | None = None


type RecoveryFixture = tuple[SoftActivationRecovery, Mock, AsyncMock, _World, Mock]


@pytest.fixture
def recovery(mocker: MockerFixture) -> RecoveryFixture:
    world = _World()
    record = SimpleNamespace(
        activation_id=_ID,
        config_generation=_GENERATION,
        target_capability_key="alpha:default",
        target_animator="alpha",
    )
    guard = Mock()
    guard.pending.return_value = record

    def clear(_record: object) -> None:
        _require_lock(world)

    guard.clear.side_effect = clear
    registry = Mock(spec=CapabilityRegistry)
    registry.get_capability.return_value = SimpleNamespace(
        animator_name="alpha",
        concurrency=SimpleNamespace(dedicated=True),
    )
    mocker.patch("lychd.system.services.runtime_recovery.capability_config_generation", return_value=_GENERATION)
    owned = OwnedBindings(
        receipt_present=True,
        generation="binding-v1",
        quadlet_sources=(Path("/owned/lychd-vessel.container"), Path("/owned/lychd-alpha.container")),
        systemd_sources=(Path("/owned/lychd-animator-alpha.target"),),
        runtime_units=(_VESSEL, _SERVICE, _TARGET),
    )
    provider = Mock(return_value=owned)
    topology = AsyncMock()
    runner = Mock()

    def observe(argv: tuple[str, ...], *, timeout_s: float) -> ProcessResult:
        _require_lock(world)
        assert timeout_s == 3
        assert argv[:2] == ("/trusted/systemctl", "--user")
        if argv[2] == "list-jobs":
            world.job_checks += 1
            content = world.jobs if world.job_checks == 1 or world.later_jobs is None else world.later_jobs
        else:
            assert argv[2] == "show"
            unit = argv[-1]
            absent_vessel = _VESSEL if world.uncaged else _OTHER_VESSEL
            if unit == absent_vessel:
                fields = {"Id": unit, "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead"}
            else:
                fields = {
                    "Id": unit,
                    "LoadState": "loaded",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "MainPID": "0",
                    "ControlPID": "0",
                    "NeedDaemonReload": "no",
                    "DropInPaths": "",
                    "FragmentPath": f"/generated/{unit}",
                    "SourcePath": f"/owned/{unit.removesuffix('.service')}.container",
                    "UnitFileState": "generated",
                }
                if unit == _TARGET or (unit == _OTHER_VESSEL and world.uncaged):
                    fields.update(FragmentPath=f"/owned/{unit}", SourcePath="", UnitFileState="static")
            if unit == world.unit and world.property_value is not None:
                fields[world.property_name] = world.property_value
            content = "".join(f"{key}={value}\n" for key, value in fields.items())
        return ProcessResult(argv=argv, returncode=0, stdout=content)

    runner.run.side_effect = observe

    @contextmanager
    def lock() -> Generator[None]:
        world.lock_held = True
        try:
            yield
        finally:
            world.lock_held = False

    service = SoftActivationRecovery(
        guard=guard,
        registry=cast("CapabilityRegistry", registry),
        topology=topology,
        owned_bindings=provider,
        runner=runner,
        systemctl_bin="/trusted/systemctl",
        systemctl_timeout_s=3,
        lock_factory=lock,
    )
    return service, guard, topology, world, provider


def _require_lock(world: _World) -> None:
    assert world.lock_held


async def test_verified_recovery_clears_only_exact_named_activation(recovery: RecoveryFixture) -> None:
    service, guard, topology, world, _ = recovery
    result = await service.recover(_ID)
    guard.clear.assert_called_once_with(result)
    topology.attest_current_topology.assert_awaited_once_with()
    assert world.job_checks == 2
    assert not world.lock_held


async def test_recovery_rejects_wrong_occurrence_before_observation(recovery: RecoveryFixture) -> None:
    service, guard, topology, _, _ = recovery
    with pytest.raises(RuntimeError, match="exact matching"):
        await service.recover("c" * 32)
    guard.clear.assert_not_called()
    topology.attest_current_topology.assert_not_awaited()


@pytest.mark.parametrize(
    ("unit", "name", "value"),
    [
        (_VESSEL, "ActiveState", "active"),
        (_VESSEL, "MainPID", "34"),
        (_OTHER_VESSEL, "LoadState", "loaded"),
        (_SERVICE, "ActiveState", "deactivating"),
        (_SERVICE, "ControlPID", "42"),
        (_SERVICE, "MainPID", "42"),
        (_TARGET, "ActiveState", "active"),
        (_SERVICE, "NeedDaemonReload", "yes"),
        (_SERVICE, "DropInPaths", "/foreign/override.conf"),
        (_VESSEL, "SourcePath", "/foreign/vessel.container"),
        (_SERVICE, "Id", "other.service"),
    ],
)
async def test_uncertain_owned_or_live_world_never_clears(
    recovery: RecoveryFixture, unit: str, name: str, value: str
) -> None:
    service, guard, _, world, _ = recovery
    world.unit, world.property_name, world.property_value = unit, name, value
    with pytest.raises(RuntimeError):
        await service.recover(_ID)
    guard.clear.assert_not_called()


@pytest.mark.parametrize("late", [False, True])
async def test_pending_jobs_before_or_after_cold_snapshot_retain_guard(
    recovery: RecoveryFixture, *, late: bool
) -> None:
    service, guard, _, world, _ = recovery
    job = f"53 {_SERVICE} start running\n"
    if late:
        world.later_jobs = job
    else:
        world.jobs = job
    with pytest.raises(RuntimeError, match="Pending systemd work"):
        await service.recover(_ID)
    guard.clear.assert_not_called()


async def test_malformed_job_observation_retains_guard(recovery: RecoveryFixture) -> None:
    service, guard, _, world, _ = recovery
    world.jobs = "unparseable jobs output\n"
    with pytest.raises(RuntimeError, match="classify pending"):
        await service.recover(_ID)
    guard.clear.assert_not_called()


async def test_changed_scribe_generation_retains_guard(recovery: RecoveryFixture) -> None:
    service, guard, _, _, provider = recovery
    provider.side_effect = [provider.return_value, OwnedBindings(receipt_present=True, generation="changed")]
    with pytest.raises(RuntimeError, match="ownership changed"):
        await service.recover(_ID)
    guard.clear.assert_not_called()


async def test_changed_config_retains_guard(recovery: RecoveryFixture, mocker: MockerFixture) -> None:
    service, guard, topology, _, _ = recovery
    mocker.patch("lychd.system.services.runtime_recovery.capability_config_generation", return_value="changed")
    with pytest.raises(RuntimeError, match="declaration changed"):
        await service.recover(_ID)
    guard.clear.assert_not_called()
    topology.attest_current_topology.assert_not_awaited()


async def test_unverified_topology_retains_guard(recovery: RecoveryFixture) -> None:
    service, guard, topology, _, _ = recovery
    topology.attest_current_topology.side_effect = RuntimeError("changed topology")
    with pytest.raises(RuntimeError, match="changed topology"):
        await service.recover(_ID)
    guard.clear.assert_not_called()


async def test_failed_exact_clear_is_reported_without_success(recovery: RecoveryFixture) -> None:
    service, guard, _, _, _ = recovery
    guard.clear.side_effect = OSError("failed durable release")
    with pytest.raises(OSError, match="durable release"):
        await service.recover(_ID)


async def test_uncaged_vessel_uses_exact_owned_plain_service(recovery: RecoveryFixture) -> None:
    service, guard, _, world, provider = recovery
    world.uncaged = True
    provider.return_value = OwnedBindings(
        receipt_present=True,
        generation="uncaged-v1",
        quadlet_sources=(Path("/owned/lychd-alpha.container"),),
        systemd_sources=(Path("/owned/lychd-uncaged-vessel.service"), Path("/owned/lychd-animator-alpha.target")),
        runtime_units=(_OTHER_VESSEL, _SERVICE, _TARGET),
    )
    result = await service.recover(_ID)
    guard.clear.assert_called_once_with(result)
