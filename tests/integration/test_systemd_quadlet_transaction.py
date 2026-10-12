"""Opt-in operator lab: real rootless Quadlet lifecycle in a private systemd manager.

This is not a Coffin containment receipt. CPU/RAM limits and an aggregate disk
quota are unavailable; scratch is measured and monitored against a 1 GiB budget.
No image export, generator, manager, or container runs without the required flag.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import select
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from lychd.config.settings.root import SettingsSnapshot
from lychd.system.host_tools import trusted_host_tool, trusted_podman_user_generator_executable
from lychd.system.unit_names import animator_service_unit, animator_target_unit
from tests.support.systemd_transaction import (
    assert_initial_topology,
    inscribe_conflicting_targets,
    manager_environment,
    read_properties,
    required_binaries,
    stop_private_manager,
    wait_for_jobs,
    wait_for_manager,
    write_harness_units,
)

pytestmark = [pytest.mark.integration, pytest.mark.container]

_IMAGE_ID = "sha256:d38d65aece7be17f39b369bd50d49f52c96f9409cb30a86cd1ce02d03e4afb8f"
_IMAGE_DIGEST = "sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca"
_BUDGET = 1024**3
_EXPORT_LIMIT = _BUDGET // 2
_WALL_SECONDS = 15 * 60
_LABEL = "org.lychd.quadlet-proof"


def _image_id(value: str) -> str:
    """Normalize only Podman's bare or explicitly SHA-256-prefixed image identity."""
    digest = value.removeprefix("sha256:")
    assert re.fullmatch(r"[0-9a-f]{64}", digest), "invalid SHA-256 image ID"
    return "sha256:" + digest


@pytest.mark.parametrize("prefix", ["", "sha256:"])
def test_image_id_accepts_only_known_prefix(prefix: str) -> None:
    assert _image_id(prefix + _IMAGE_ID.removeprefix("sha256:")) == _IMAGE_ID


@pytest.mark.parametrize(
    "value", ["a" * 63, "a" * 65, "sha512:" + "a" * 64, "sha256:" * 2 + "a" * 64, "z" * 64, "python:latest"]
)
def test_image_id_refuses_malformed_identity(value: str) -> None:
    with pytest.raises(AssertionError, match="invalid SHA-256 image ID"):
        _image_id(value)


def _scratch_bytes(root: Path) -> int:
    """Measure allocated or apparent bytes without following store symlinks."""
    total = 0
    for directory, _children, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                metadata = (Path(directory) / name).lstat()
            except FileNotFoundError:
                continue
            total += max(metadata.st_size, metadata.st_blocks * 512)
    return total


class Lab:
    """Fixed-deadline subprocess ledger with bounded diagnostics and scratch checks."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.started = time.monotonic()
        self.deadline = self.started + _WALL_SECONDS - 60
        self.commands: list[dict[str, object]] = []
        self.peak_scratch = 0
        self.process_handles: dict[int, int] = {}

    def budget(self) -> None:
        self.peak_scratch = max(self.peak_scratch, _scratch_bytes(self.root))
        assert self.peak_scratch <= _BUDGET, "scratch budget exceeded; cleanup required"
        assert time.monotonic() < self.deadline, "lab wall deadline reached; cleanup required"

    def run(  # noqa: PLR0915 - one auditable subprocess/resource lifetime
        self,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        *,
        timeout: float = 30,
        acceptable: tuple[int, ...] = (0,),
        cleanup: bool = False,
        require_clean_stderr: bool = False,
    ) -> str:
        """Keep bounded stdout separate from diagnostics; terminate owned groups on failure."""
        if not cleanup:
            self.budget()
        began = time.monotonic()
        deadline = min(began + timeout, self.started + _WALL_SECONDS if cleanup else self.deadline)
        output = self.root / f"command-{len(self.commands):03d}.stdout.log"
        errors = self.root / f"command-{len(self.commands):03d}.stderr.log"
        record: dict[str, object] = {
            "argv": list(arguments),
            "began": began,
            "stdout_file": str(output),
            "stderr_file": str(errors),
        }
        self.commands.append(record)
        with output.open("wb") as stream, errors.open("wb") as error_stream, selectors.DefaultSelector() as reader:
            process = subprocess.Popen(  # noqa: S603 - fixed lab argv; no shell or ambient command input
                arguments,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdout is not None
            assert process.stderr is not None
            reader.register(process.stdout, selectors.EVENT_READ)
            reader.register(process.stderr, selectors.EVENT_READ)
            retained = {"stdout": 0, "stderr": 0}
            truncated = {"stdout": False, "stderr": False}
            finished = False
            try:
                # WNOWAIT leaves the leader unreaped, reserving its PID/session
                # identity until any exceptional group termination is complete.
                while (
                    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None or reader.get_map()
                ):
                    assert time.monotonic() < deadline, f"command timed out: {arguments[0]}"
                    if not cleanup:
                        self.budget()
                    for key, _events in reader.select(timeout=0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            reader.unregister(key.fd)
                            continue
                        channel = "stdout" if key.fileobj is process.stdout else "stderr"
                        destination = stream if channel == "stdout" else error_stream
                        bounded = chunk[: max(0, 65536 - retained[channel])]
                        destination.write(bounded)
                        retained[channel] += len(bounded)
                        truncated[channel] |= len(bounded) < len(chunk)
                finished = True
            finally:
                if not finished:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
                process.stdout.close()
                process.stderr.close()
                record.update(exit=process.returncode, seconds=time.monotonic() - began, truncated=truncated)
        text = output.read_text(encoding="utf-8", errors="replace")
        stderr = errors.read_text(encoding="utf-8", errors="replace")
        diagnostic = f"stdout:\n{text}\nstderr:\n{stderr}"
        assert process.returncode in acceptable, diagnostic
        assert not truncated["stdout"], "refusing truncated command stdout: " + diagnostic
        if require_clean_stderr:
            assert not stderr, "engine emitted diagnostics: " + diagnostic
        if not cleanup:
            self.budget()
        return text.strip()


def test_lab_separates_structured_stdout_and_bounded_stderr(tmp_path: Path) -> None:
    """Preserve diagnostics without corrupting values returned by an authored probe."""
    lab = Lab(tmp_path)
    output = lab.run(
        [sys.executable, "-I", "-B", "-c", "import sys; print('true v2 systemd'); sys.stderr.write('x' * 70000)"],
        {},
    )
    assert output == "true v2 systemd"
    assert (tmp_path / "command-000.stderr.log").stat().st_size == 65536
    assert lab.commands[-1]["truncated"] == {"stdout": False, "stderr": True}


def test_lab_refuses_engine_diagnostics_even_with_expected_stdout(tmp_path: Path) -> None:
    """A warning cannot disappear merely because structured stdout looks acceptable."""
    with pytest.raises(AssertionError, match="engine emitted diagnostics"):
        Lab(tmp_path).run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; print('true v2 systemd'); print('fallback warning', file=sys.stderr)",
            ],
            {},
            require_clean_stderr=True,
        )


def _archive_bound(path: Path) -> int:
    """Validate the exact OCI image without extracting and bound VFS layer copies."""
    assert path.stat().st_size <= _EXPORT_LIMIT
    with tarfile.open(path, mode="r:") as archive:
        members = archive.getmembers()
        assert len(members) <= 256, "unexpected OCI outer archive member count"
        for member in members:
            name = Path(member.name)
            assert not name.is_absolute()
            assert ".." not in name.parts
            assert member.isfile() or member.isdir(), "outer OCI links/devices are forbidden"

        def blob(name: str, maximum: int = 1024**2) -> bytes:
            item = archive.getmember(name)
            assert item.size <= maximum
            stream = archive.extractfile(item)
            assert stream is not None
            return stream.read(maximum + 1)

        index = json.loads(blob("index.json"))
        assert len(index["manifests"]) == 1
        manifest_digest = index["manifests"][0]["digest"]
        manifest_bytes = blob("blobs/sha256/" + manifest_digest.removeprefix("sha256:"))
        assert "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() == manifest_digest
        manifest = json.loads(manifest_bytes)
        assert manifest["config"]["digest"] == _IMAGE_ID
        config = blob("blobs/sha256/" + _IMAGE_ID.removeprefix("sha256:"))
        assert "sha256:" + hashlib.sha256(config).hexdigest() == _IMAGE_ID
        cumulative = 0
        vfs_layers = 0
        file_count = 0
        for layer in manifest["layers"]:
            assert layer["digest"].startswith("sha256:")
            stream = archive.extractfile("blobs/sha256/" + layer["digest"].removeprefix("sha256:"))
            assert stream is not None
            with tarfile.open(fileobj=stream, mode="r|*") as layer_tar:
                for member in layer_tar:
                    file_count += 1
                    assert file_count <= 100000, "image has too many layer members"
                    cumulative += max(member.size, 4096)
                    assert cumulative <= _BUDGET, "image expansion exceeds budget"
            vfs_layers += cumulative
        # Conservative additive layers (no whiteout credit), two container copies,
        # one load working copy, archive, and 64 MiB metadata/log headroom.
        bound = vfs_layers + 3 * cumulative + path.stat().st_size + 64 * 1024**2
        assert bound <= _BUDGET, f"estimated VFS scratch {bound} exceeds {_BUDGET}"
        return bound


def _quadlet(
    *,
    image: str,
    container: str,
    target: str,
    token: str,
    global_arguments: Sequence[str],
    prerequisite: Path | None,
) -> str:
    """Author the entire admitted fixture: no ports, GPUs, mounts, or credentials."""
    precondition = f"ExecStartPre={sys.executable} -I -B {prerequisite}\n" if prerequisite else ""
    return (
        f"[Unit]\nDescription=LychD isolated proof {container}\nBindsTo={target}\nAfter={target}\n"
        "[Container]\n"
        f"Image={image}\nContainerName={container}\nLabel={_LABEL}={token}\n"
        "Pull=never\nNetwork=none\nHttpProxy=false\nReadOnly=true\nReadOnlyTmpfs=false\n"
        "NoNewPrivileges=true\nDropCapability=ALL\nPidsLimit=64\nStopTimeout=2\n"
        f"GlobalArgs={' '.join(global_arguments)}\n"
        # PID 1 must handle SIGTERM explicitly; otherwise the sleeping fixture
        # requires SIGKILL and reports failure despite a correctly ordered stop.
        'Exec=/usr/local/bin/python3 -I -B -c "import signal, sys, time; '
        'signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(45)"\n'
        "[Service]\nRestart=no\nTimeoutStartSec=20\nTimeoutStopSec=5\nRuntimeMaxSec=60\n"
        f"{precondition}"
    )


def _remember_processes(lab: Lab, podman: Sequence[str], environment: Mapping[str, str], name: str, token: str) -> None:
    """Open observation-only pidfds from an exact, currently owned container."""
    observed = lab.run(
        [
            *podman,
            "inspect",
            "--format",
            '{{.Id}}|{{index .Config.Labels "' + _LABEL + '"}}|{{.State.Pid}}|{{.ConmonPidFile}}',
            name,
        ],
        environment,
        cleanup=True,
    ).split("|")
    assert len(observed) == 4
    assert observed[1] == token, "refusing foreign container process observation"
    conmon_file = Path(observed[3]).resolve()
    assert conmon_file.is_relative_to(lab.root), "conmon pidfile escaped isolated store"
    pids = [int(observed[2])]
    if conmon_file.exists():
        pids.append(int(conmon_file.read_text()[:32]))
    for pid in pids:
        if pid > 0 and pid not in lab.process_handles:
            with suppress(ProcessLookupError):
                lab.process_handles[pid] = os.pidfd_open(pid)


def _assert_container(lab: Lab, podman: Sequence[str], environment: Mapping[str, str], name: str, token: str) -> None:
    """Attest the selected container surface without reading its environment."""
    _remember_processes(lab, podman, environment, name, token)
    observed = lab.run(
        [
            *podman,
            "inspect",
            "--format",
            "{{.Image}} {{.State.Running}} {{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.PidsLimit}} {{len .Mounts}}",
            name,
        ],
        environment,
    )
    fields = observed.split()
    assert len(fields) == 6, observed
    assert _image_id(fields[0]) == _IMAGE_ID, observed
    assert fields[1:] == ["true", "none", "true", "64", "0"], observed


def _verify_process_exit(lab: Lab) -> list[str]:
    """Wait on captured kernel handles; never signal a PID read from a file."""
    failures: list[str] = []
    for pid, handle in lab.process_handles.items():
        try:
            ready, _, _ = select.select([handle], [], [], 5)
            if not ready:
                failures.append(f"observed container/conmon PID {pid} did not exit")
        finally:
            os.close(handle)
    return failures


def _capture_before_stop(
    lab: Lab, podman: Sequence[str], environment: Mapping[str, str], names: Sequence[str], token: str
) -> list[str]:
    """Capture remaining owned container/helper handles before target stop removes metadata."""
    failures: list[str] = []
    for name in names:
        try:
            lab.run([*podman, "container", "exists", name], environment, acceptable=(0, 1), cleanup=True)
            if lab.commands[-1]["exit"] == 0:
                _remember_processes(lab, podman, environment, name, token)
        except (AssertionError, OSError, ValueError) as exc:
            failures.append(str(exc))
    return failures


def _cleanup(
    lab: Lab,
    *,
    manager: subprocess.Popen[str] | None,
    environment: Mapping[str, str],
    binaries: Mapping[str, Path],
    podman: Sequence[str],
    targets: Sequence[str],
    names: Sequence[str],
    token: str,
    engine_started: bool,
) -> None:
    """Stop exact private targets; verify ownership before removing remaining objects."""
    failures = _capture_before_stop(lab, podman, environment, names, token) if engine_started else []
    try:
        if manager is not None and manager.poll() is None:
            try:
                lab.run(
                    [str(binaries["systemctl"]), "--user", "stop", *targets],
                    environment,
                    timeout=10,
                    cleanup=True,
                )
            except (AssertionError, OSError) as exc:
                failures.append(str(exc))
        for name in names if engine_started else ():
            try:
                present = lab.run([*podman, "container", "exists", name], environment, acceptable=(0, 1), cleanup=True)
                del present
                if lab.commands[-1]["exit"] == 1:
                    continue
                _remember_processes(lab, podman, environment, name, token)
                actual = lab.run(
                    [*podman, "inspect", "--format", '{{.Id}} {{index .Config.Labels "' + _LABEL + '"}}', name],
                    environment,
                    cleanup=True,
                ).split()
                assert len(actual) == 2, "cleanup ownership mismatch"
                assert actual[1] == token, "cleanup ownership mismatch"
                identifier = actual[0]
                lab.run([*podman, "stop", "--time=2", identifier], environment, timeout=5, cleanup=True)
                running = lab.run(
                    [*podman, "inspect", "--format", "{{.State.Running}}", identifier], environment, cleanup=True
                )
                assert running == "false", "refusing to remove a running container"
                lab.run([*podman, "rm", identifier], environment, timeout=5, cleanup=True)
            except (AssertionError, OSError) as exc:
                failures.append(str(exc))
        for name in names if engine_started else ():
            try:
                lab.run([*podman, "container", "exists", name], environment, acceptable=(1,), cleanup=True)
            except (AssertionError, OSError) as exc:
                failures.append(str(exc))
    finally:
        if manager is not None:
            stop_private_manager(manager, Path(environment["XDG_RUNTIME_DIR"]))
            assert manager.poll() is not None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                private_socket = str(Path(environment["XDG_RUNTIME_DIR"]) / "systemd/private")
                assert probe.connect_ex(private_socket) != 0, "private manager socket still accepts connections"
    failures.extend(_verify_process_exit(lab))
    assert not failures, "cleanup incomplete; preserve task root: " + "; ".join(failures)


def test_real_quadlet_conflict_transaction(tmp_path: Path) -> None:  # noqa: PLR0915 - explicit effect sequence
    """Require a real offline OCI lifecycle, or fail explicitly when proof is requested."""
    if os.environ.get("LYCHD_REQUIRE_QUADLET_PROOF") != "1":
        pytest.skip("real rootless Quadlet proof requires LYCHD_REQUIRE_QUADLET_PROOF=1")
    assert sys.platform == "linux"
    assert os.getuid() != 0
    binaries = required_binaries()
    podman_path = trusted_host_tool("podman")
    limiter = trusted_host_tool("prlimit", fallbacks=(Path("/usr/bin/prlimit"),))
    generator = trusted_podman_user_generator_executable()
    assert podman_path, "root-controlled Podman required"
    assert limiter, "root-controlled prlimit required"
    assert generator, "root-controlled Quadlet required"
    token = uuid.uuid4().hex[:10]
    lab = Lab(tmp_path)
    assert re.fullmatch(r"[A-Za-z0-9_./-]+", str(tmp_path)), "lab paths must be unambiguous in Quadlet argv"
    names = (f"proof-{token}-alpha", f"proof-{token}-beta")
    targets = tuple(animator_target_unit(name) for name in names)
    services = tuple(animator_service_unit(name) for name in names)
    container_names = tuple(f"lychd-{name}" for name in names)
    targets_dir, discarded, inputs, generated, support = (
        tmp_path / name for name in ("targets", "unconsumed-source-quadlets", "input", "generated", "support")
    )
    for directory in (targets_dir, discarded, inputs, generated, support):
        directory.mkdir()
    environment = manager_environment(tmp_path / "manager", targets_dir, support, inherit_environment=False)
    assert len(str(Path(environment["XDG_RUNTIME_DIR"]) / "systemd/private").encode()) < 104, (
        "use a shorter pytest basetemp"
    )
    environment.update(
        SYSTEMD_UNIT_PATH=f"{targets_dir}:{generated}:{support}",
        DBUS_SESSION_BUS_ADDRESS=f"unix:path={environment['XDG_RUNTIME_DIR']}/systemd/private",
        TMPDIR=str(tmp_path / "temporary"),
    )
    Path(environment["TMPDIR"]).mkdir()
    # Explicit empty local configuration prevents discovery of operator containers
    # configuration/auth/proxies. The source cache read uses only the host identity.
    containers_config = tmp_path / "containers.conf"
    containers_config.write_text('[engine]\nevents_logger="file"\n', encoding="utf-8")
    registries_config = tmp_path / "registries.conf"
    registries_config.write_text("unqualified-search-registries=[]\n", encoding="utf-8")
    authfile = tmp_path / "auth.json"
    authfile.write_text('{"auths":{}}\n', encoding="utf-8")
    environment.update(
        CONTAINERS_CONF=str(containers_config),
        CONTAINERS_REGISTRIES_CONF=str(registries_config),
        REGISTRY_AUTH_FILE=str(authfile),
    )
    host_identity = pwd.getpwuid(os.getuid())
    # Pin source engine paths through ordinary rootless defaults. Explicit config
    # overrides above still apply; no inherited registry/proxy/credential variables.
    source_runtime = Path(f"/run/user/{os.getuid()}")
    source_bus = source_runtime / "bus"
    source_bus_metadata = source_bus.stat()
    assert stat.S_ISSOCK(source_bus_metadata.st_mode), "ordinary user session bus is unavailable"
    assert source_bus_metadata.st_uid == os.getuid(), "ordinary user session bus has the wrong owner"
    source_environment = {
        **environment,
        "HOME": host_identity.pw_dir,
        "XDG_RUNTIME_DIR": str(source_runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={source_bus}",
        "XDG_DATA_HOME": str(Path(host_identity.pw_dir) / ".local/share"),
    }
    private_storage = tmp_path / "storage.conf"
    private_storage.write_text(
        f'[storage]\ndriver="vfs"\ngraphroot="{tmp_path / "store"}"\nrunroot="{tmp_path / "runroot"}"\n',
        encoding="utf-8",
    )
    environment["CONTAINERS_STORAGE_CONF"] = str(private_storage)
    environment["PODMAN_NO_PAUSE_PROCESS"] = "1"
    # Rootless mounts.conf overrides the system secret-copy defaults.
    for location in (
        Path(environment["HOME"]) / ".config/containers",
        Path(environment["XDG_CONFIG_HOME"]) / "containers",
    ):
        location.mkdir(parents=True, exist_ok=True)
        (location / "mounts.conf").write_text("", encoding="utf-8")
    source_info = lab.run(
        [
            podman_path,
            "info",
            "--format",
            "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupManager}}",
        ],
        source_environment,
        require_clean_stderr=True,
    )
    assert source_info == "true v2 systemd", source_info
    cached = lab.run(
        [podman_path, "image", "inspect", "--format", "{{.Id}} {{.Digest}}", _IMAGE_ID], source_environment
    )
    cached_fields = cached.split()
    assert len(cached_fields) == 2, "unexpected cached image identity output"
    assert _image_id(cached_fields[0]) == _IMAGE_ID, "cached image identity changed"
    assert cached_fields[1] == _IMAGE_DIGEST, "cached manifest digest changed"
    archive = tmp_path / "python.oci.tar"
    lab.run(
        [
            limiter,
            f"--fsize={_EXPORT_LIMIT}:{_EXPORT_LIMIT}",
            "--",
            podman_path,
            "save",
            "--format=oci-archive",
            "--output",
            str(archive),
            _IMAGE_ID,
        ],
        source_environment,
        timeout=120,
    )
    estimate = _archive_bound(archive)
    global_arguments = (
        f"--root={tmp_path / 'store'}",
        f"--runroot={tmp_path / 'runroot'}",
        "--storage-driver=vfs",
        "--cgroup-manager=systemd",
        f"--tmpdir={tmp_path / 'engine-tmp'}",
    )
    podman = (podman_path, *global_arguments)
    settings = SettingsSnapshot(
        payload=json.dumps(
            {
                "orchestration": {"switching": {"host_reactor_dir": str(tmp_path / "control/inbox")}},
                "extensions": {"builtins": [], "crypt": []},
            }
        )
    ).materialize()
    inscribe_conflicting_targets(
        generated_dir=targets_dir, quadlet_dir=discarded, alpha_name=names[0], beta_name=names[1], settings=settings
    )
    write_harness_units(
        support,
        alpha_target=targets[0],
        alpha_service=services[0],
        beta_target=targets[1],
        beta_service=services[1],
        marker_name="unused",
        binaries=binaries,
        inert_services=False,
    )
    observation = tmp_path / "alpha-cold-before-beta.json"
    alpha_start_bound = tmp_path / "alpha-start-lower-bound"
    prerequisite = tmp_path / "check_alpha_absent.py"
    prerequisite.write_text(
        "import json, subprocess, time\n"
        f"with open({str(alpha_start_bound)!r}) as stream: alpha_started = float(stream.read())\n"
        f"result = subprocess.run({[*podman, 'container', 'exists', container_names[0]]!r}, timeout=3, check=False, capture_output=True)\n"
        "if result.returncode != 1: raise SystemExit('alpha was not absent before beta start')\n"
        "elapsed = time.monotonic() - alpha_started\n"
        "if not 0 <= elapsed < 30: raise SystemExit('ordering oracle exceeded 30 seconds')\n"
        f"with open({str(observation)!r}, 'w') as stream: json.dump({{'alpha_absent': True, 'elapsed_since_alpha_start_lower_bound': elapsed, 'monotonic': time.monotonic()}}, stream)\n",
        encoding="utf-8",
    )
    for index, service in enumerate(services):
        (inputs / service.replace(".service", ".container")).write_text(
            _quadlet(
                image=_IMAGE_ID,
                container=container_names[index],
                target=targets[index],
                token=token,
                global_arguments=global_arguments,
                prerequisite=prerequisite if index else None,
            ),
            encoding="utf-8",
        )
    assert sorted(path.name for path in inputs.iterdir()) == sorted(
        service.replace(".service", ".container") for service in services
    )
    lab.run([generator.path, "--user", str(generated)], {**environment, "QUADLET_UNIT_DIRS": str(inputs)})
    assert sorted(path.name for path in generated.iterdir()) == sorted(services)
    for service in services:
        text = (generated / service).read_text(encoding="utf-8")
        start_lines = [line.removeprefix("ExecStart=") for line in text.splitlines() if line.startswith("ExecStart=")]
        assert len(start_lines) == 1, "expected one generated container start"
        start = start_lines[0]
        for required in (*global_arguments, _IMAGE_ID):
            assert required in start, f"generated service lost admitted option: {required}"
        # Installed Quadlet may emit either --key=value or --key value. Require
        # the same exact value at argument boundaries in the actual start command.
        for option, value in (("network", "none"), ("pull", "never"), ("pids-limit", "64")):
            assert re.search(rf"(?:^| )--{option}(?:=| ){value}(?: |$)", start), option
        for forbidden in ("--privileged", "--publish", "--volume"):
            assert forbidden not in start
    manager: subprocess.Popen[str] | None = None
    manager_log = tmp_path / "manager.log"
    completed = False
    cleanup_ok = False
    engine_started = False
    try:
        with manager_log.open("w") as stream:
            manager = subprocess.Popen(  # noqa: S603 - root-controlled manager; explicit isolated environment
                [
                    str(binaries["systemd"]),
                    "--user",
                    "--unit=basic.target",
                    "--log-target=console",
                    "--log-level=warning",
                    "--no-pager",
                ],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        unavailable = wait_for_manager(manager, systemctl=binaries["systemctl"], environment=environment)
        assert not unavailable, f"private manager unsupported; no fallback: {unavailable}"
        engine_started = True
        storage = lab.run(
            [*podman, "info", "--format", "{{.Store.GraphRoot}} {{.Store.RunRoot}} {{.Store.GraphDriverName}}"],
            environment,
        )
        assert storage == f"{tmp_path / 'store'} {tmp_path / 'runroot'} vfs", storage
        lab.run([*podman, "load", "--input", str(archive)], environment, timeout=120)
        loaded_id = lab.run([*podman, "image", "inspect", "--format", "{{.Id}}", _IMAGE_ID], environment)
        assert _image_id(loaded_id) == _IMAGE_ID
        info = lab.run(
            [
                *podman,
                "info",
                "--format",
                "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupManager}}",
            ],
            environment,
        )
        assert info == "true v2 systemd", info
        alpha_start_bound.write_text(str(time.monotonic()), encoding="utf-8")
        lab.run([str(binaries["systemctl"]), "--user", "start", targets[0]], environment, timeout=25)
        initial = read_properties(systemctl=binaries["systemctl"], environment=environment, units=(*targets, *services))
        assert_initial_topology(
            initial,
            alpha_target=targets[0],
            alpha_service=services[0],
            beta_target=targets[1],
            beta_service=services[1],
            generated_dir=targets_dir,
            harness_dir=generated,
            manager_log=manager_log,
        )
        _assert_container(lab, podman, environment, container_names[0], token)
        lab.run([str(binaries["systemctl"]), "--user", "start", "--job-mode=fail", targets[1]], environment, timeout=25)
        assert not wait_for_jobs(systemctl=binaries["systemctl"], environment=environment, unique_stem=token)
        final = read_properties(systemctl=binaries["systemctl"], environment=environment, units=(*targets, *services))
        lab.run(
            [str(binaries["systemctl"]), "--user", "show", "--property=Result,ExecMainStatus,ExecMainCode", *services],
            environment,
        )
        assert final[targets[0]]["ActiveState"] == final[services[0]]["ActiveState"] == "inactive"
        assert final[targets[1]]["ActiveState"] == final[services[1]]["ActiveState"] == "active"
        assert final[services[1]]["SubState"] == "running"
        ordering = json.loads(observation.read_text())
        assert ordering["alpha_absent"] is True
        assert ordering["elapsed_since_alpha_start_lower_bound"] < 30
        _assert_container(lab, podman, environment, container_names[1], token)
        lab.budget()
        completed = True
    finally:
        try:
            _cleanup(
                lab,
                manager=manager,
                environment=environment,
                binaries=binaries,
                podman=podman,
                targets=targets,
                names=container_names,
                token=token,
                engine_started=engine_started,
            )
            cleanup_ok = True
        finally:
            with archive.open("rb") as archive_stream:
                archive_digest = hashlib.file_digest(archive_stream, "sha256").hexdigest()
            receipt: dict[str, Any] = {
                "passed": completed and cleanup_ok,
                "private_engine_no_pause_process": True,
                "observed_process_pids": list(lab.process_handles),
                "cleanup_complete": cleanup_ok,
                "image_id": _IMAGE_ID,
                "cached_manifest_digest": _IMAGE_DIGEST,
                "archive_sha256": archive_digest,
                "estimated_scratch": estimate,
                "observed_peak_scratch": lab.peak_scratch,
                "seconds": time.monotonic() - lab.started,
                "commands": lab.commands,
                "limitations": "Operator lab; no CPU/RAM or hard aggregate-disk quota; no GPU/model/orchestrator receipt.",
            }
            (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
