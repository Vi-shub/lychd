"""Generated Vessel storage preserves containment under either actuator profile."""

import pytest

from lychd.config.settings.root import Settings
from lychd.domain.animation.services.adapters.registry import RuntimeAdapterRegistry
from lychd.domain.animation.transmute import Transmuter
from lychd.system import constants
from lychd.system.schemas import QuadletContainer


@pytest.mark.parametrize("actuator", ["host-reactor", "systemd"])
def test_vessel_has_a_dedicated_persistent_soft_activation_mount(actuator: str) -> None:
    settings = Settings.model_validate({"orchestration": {"switching": {"actuator": actuator}}})
    manifests = Transmuter(settings=settings, runtime_planner=RuntimeAdapterRegistry()).transmute_all([])
    containers = {item.container_name: item for item in manifests if isinstance(item, QuadletContainer)}
    guard_path = constants.PATH_RUNTIME_GUARD_DIR
    assert guard_path == constants.PATH_CRYPT_ROOT / "runtime-guard"
    assert guard_path in constants.HOST_LAYOUT
    vessel = containers["lychd-vessel"]
    mounts = [mount for mount in vessel.volumes if mount.host_path == guard_path]
    assert len(mounts) == 1
    assert mounts[0].container_path == guard_path
    assert mounts[0].options == ["rw", "Z"]
    assert vessel.read_only
    for name, container in containers.items():
        if name != "lychd-vessel":
            assert not any(mount.host_path == guard_path for mount in container.volumes)
