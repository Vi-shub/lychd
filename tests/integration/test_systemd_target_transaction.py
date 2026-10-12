"""Hermetic receipt for LychD's generated systemd target transaction."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from lychd.config.settings.root import get_settings
from lychd.system.unit_names import animator_service_unit, animator_target_unit
from tests.support.systemd_transaction import (
    assert_initial_topology,
    diagnostics,
    inscribe_conflicting_targets,
    manager_environment,
    read_properties,
    required_binaries,
    run_systemctl,
    stop_private_manager,
    wait_for_jobs,
    wait_for_manager,
    write_harness_units,
)
from tests.support.systemd_transaction import (
    unavailable as proof_unavailable,
)

pytestmark = pytest.mark.integration


def test_generated_conflict_targets_execute_one_ordered_systemd_transaction(tmp_path: Path) -> None:
    """Prove target relations and conflict switching in an isolated real manager."""
    if sys.platform != "linux":
        proof_unavailable("the real systemd transaction receipt requires Linux")
    binaries = required_binaries()

    token = uuid.uuid4().hex[:10]
    alpha_name = f"proof-{token}-alpha"
    beta_name = f"proof-{token}-beta"
    alpha_target = animator_target_unit(alpha_name)
    beta_target = animator_target_unit(beta_name)
    alpha_service = animator_service_unit(alpha_name)
    beta_service = animator_service_unit(beta_name)
    managed_units = frozenset((alpha_target, alpha_service, beta_target, beta_service))
    marker_name = f"lychd-{token}-alpha-stopped"

    generated_dir = tmp_path / "generated"
    quadlet_dir = tmp_path / "quadlet"
    harness_dir = tmp_path / "harness"
    for directory in (generated_dir, quadlet_dir, harness_dir):
        directory.mkdir()
    inscribe_conflicting_targets(
        generated_dir=generated_dir,
        quadlet_dir=quadlet_dir,
        alpha_name=alpha_name,
        beta_name=beta_name,
        settings=get_settings(),
    )
    write_harness_units(
        harness_dir,
        alpha_target=alpha_target,
        alpha_service=alpha_service,
        beta_target=beta_target,
        beta_service=beta_service,
        marker_name=marker_name,
        binaries=binaries,
    )

    manager_temp = tempfile.TemporaryDirectory(prefix=f"lychd-systemd-{token}-")
    environment = manager_environment(Path(manager_temp.name) / "manager", generated_dir, harness_dir)
    runtime_dir = Path(environment["XDG_RUNTIME_DIR"])
    manager_log = tmp_path / "systemd-manager.log"
    with manager_log.open("w", encoding="utf-8") as log_stream:
        try:
            manager = subprocess.Popen(  # noqa: S603 - the absolute executable is root-owned and pre-attested
                [
                    str(binaries["systemd"]),
                    "--user",
                    f"--unit={alpha_target}",
                    "--log-target=console",
                    "--log-level=warning",
                    "--no-pager",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            manager_temp.cleanup()
            proof_unavailable(f"isolated user manager could not start: {exc}")
        try:
            unavailable = wait_for_manager(
                manager,
                systemctl=binaries["systemctl"],
                environment=environment,
            )
            if unavailable:
                proof_unavailable(f"isolated user manager is unavailable: {unavailable}")

            unsettled = wait_for_jobs(
                systemctl=binaries["systemctl"],
                environment=environment,
                unique_stem=token,
            )
            assert not unsettled, f"{unsettled}\n{diagnostics(manager_log=manager_log)}"

            initial = read_properties(
                systemctl=binaries["systemctl"],
                environment=environment,
                units=tuple(managed_units),
            )
            assert_initial_topology(
                initial,
                alpha_target=alpha_target,
                alpha_service=alpha_service,
                beta_target=beta_target,
                beta_service=beta_service,
                generated_dir=generated_dir,
                harness_dir=harness_dir,
                manager_log=manager_log,
            )

            switched = run_systemctl(
                binaries["systemctl"],
                environment,
                "start",
                "--job-mode=fail",
                beta_target,
                timeout=5.0,
            )
            assert switched.returncode == 0, diagnostics(
                manager_log=manager_log,
                properties=initial,
                command=switched,
            )
            unsettled = wait_for_jobs(
                systemctl=binaries["systemctl"],
                environment=environment,
                unique_stem=token,
            )
            assert not unsettled, f"{unsettled}\n{diagnostics(manager_log=manager_log)}"

            final = read_properties(
                systemctl=binaries["systemctl"],
                environment=environment,
                units=tuple(managed_units),
            )
            states = {unit: (snapshot["ActiveState"], snapshot["SubState"]) for unit, snapshot in final.items()}
            assert states == {
                alpha_target: ("inactive", "dead"),
                alpha_service: ("inactive", "dead"),
                beta_target: ("active", "active"),
                beta_service: ("active", "exited"),
            }, diagnostics(manager_log=manager_log, properties=final)
            assert (runtime_dir / marker_name).is_file(), diagnostics(
                manager_log=manager_log,
                properties=final,
            )
        finally:
            stop_private_manager(manager, runtime_dir)
            manager_temp.cleanup()
