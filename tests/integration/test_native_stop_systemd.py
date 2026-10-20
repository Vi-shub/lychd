"""Opt-in native stop settlement through an isolated, real systemd user manager.

One finite proof runs required-close failure, clean closure, and reference loss. It uses
the actual generated native unit, CLI, owned lifespan, signed HTTP admission,
Scribe ownership, inventory and controller. The clean inert case also checks public
start against an active foreign counterpart, then bootstraps only the owned native
Vessel after the harness stops that counterpart. The explicit inert factory and local
installation shim do not qualify an installed production graph or model body.
The separate retained Scavenger case keeps Root-owned PostgreSQL external and
qualifies only idle production publication and native Vessel stop/restart. It
does not qualify public start, installed lifetime, or real dependency shutdown.
No host user-manager fallback, installed units, or inherited environment is used.
Receipts and credentials belong to an explicit basetemp outside the checkout.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from dbus_fast.aio import MessageBus
from pydantic import SecretStr

from lychd.config.settings.root import SettingsSnapshot
from lychd.system.operator import stop_evidence
from lychd.system.operator.admission_client import StopAdmissionClient
from lychd.system.operator.control import OperatorControlService
from lychd.system.operator.inventory import ConfiguredAnimatorDeclarations, OperatorInventoryService, OperatorPaths
from lychd.system.operator.models import (
    ObservationState,
    OperatorAction,
    OperatorAuthorityError,
    OperatorError,
    OperatorTarget,
    VesselAuthority,
)
from lychd.system.operator.process import ProcessInvocationError, ProcessResult
from lychd.system.operator.storage import StorageInventoryService
from lychd.system.operator.targets import OperatorTargetResolver
from lychd.system.operator.units import OwnedUnitInventoryService
from lychd.system.schemas import systemd_executable_token
from lychd.system.services import bind_compilation
from lychd.system.services.lifecycle.lock import LifecycleLock
from lychd.system.services.lifecycle.receipt import LifecycleReceiptStore
from lychd.system.services.local_access import LocalAccess
from lychd.system.services.scribe import ScribeService
from tests.fixtures.web.native_scavenger_app import RetainedScavenger
from tests.support.native_stop_bus import NativeStopBus, broker_binaries, prepare_bus
from tests.support.systemd_transaction import (
    manager_environment,
    read_properties,
    require_delegated_manager_cgroup,
    required_binaries,
    run_systemctl,
    stop_private_manager,
    wait_for_jobs,
    wait_for_manager,
)
from tests.support.systemd_transaction import unavailable as proof_unavailable

pytestmark = pytest.mark.integration

_VESSEL = "lychd-uncaged-vessel.service"
_COUNTERPART = "lychd-vessel.service"
_COUNTERPART_PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "ControlPID", "Job")
_REPOSITORY = Path(__file__).resolve().parents[2]
_FIXTURE = _REPOSITORY / "tests/fixtures/web/systemd_server_app.py"
_SCAVENGER_FIXTURE = _REPOSITORY / "tests/fixtures/web/native_scavenger_app.py"
_CONTROL_OUTER_SECONDS = 45.0
_LISTENER_SECONDS = 10.0
_OBSERVATION_SECONDS = 5.0
_OUTPUT_BYTES = 65536
_NATIVE_LOG_BYTES = 2 * 1024 * 1024
_NATIVE_LOG_PREFIX = b"LYCHD_NATIVE_PROOF "
_SOURCES = (
    "pyproject.toml",
    "uv.lock",
    "src/lychd/__main__.py",
    "src/lychd/app.py",
    "src/lychd/config/constants.py",
    "src/lychd/interface/server.py",
    "src/lychd/interface/server_policy.py",
    "src/lychd/interface/api/lifecycle.py",
    "src/lychd/domain/codex/local_access.py",
    "src/lychd/system/operator/admission.py",
    "src/lychd/system/operator/admission_client.py",
    "src/lychd/system/operator/control.py",
    "src/lychd/system/operator/stop_evidence.py",
    "src/lychd/system/operator/units.py",
    "src/lychd/system/operator/inventory.py",
    "src/lychd/system/operator/targets.py",
    "src/lychd/system/services/bind_compilation.py",
    "src/lychd/system/services/lifecycle/lock.py",
    "src/lychd/system/schemas.py",
    "tests/support/systemd_transaction.py",
    "tests/support/native_stop_bus.py",
    "tests/fixtures/web/systemd_server_app.py",
    "tests/fixtures/web/native_scavenger_app.py",
    "tests/integration/test_native_stop_systemd.py",
)
_CLEAN_EXIT = {
    "Id": _VESSEL,
    "LoadState": "loaded",
    "ActiveState": "inactive",
    "SubState": "dead",
    "MainPID": "0",
    "ControlPID": "0",
    "Result": "success",
    "ExecMainCode": "1",
    "ExecMainStatus": "0",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


@dataclass(frozen=True)
class _Process:
    pid: int
    starttime: int
    pgid: int
    session: int
    parent: int
    state: str
    cgroup: str


def _process(pid: int) -> _Process | None:
    try:
        directory = Path("/proc") / str(pid)
        assert directory.stat().st_uid == os.getuid()
        fields = (directory / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return _Process(
            pid=pid,
            starttime=int(fields[19]),
            pgid=int(fields[2]),
            session=int(fields[3]),
            parent=int(fields[1]),
            state=fields[0],
            cgroup=(directory / "cgroup").read_text(encoding="ascii"),
        )
    except (FileNotFoundError, ProcessLookupError):
        return None


def _same_process(expected: _Process) -> bool:
    actual = _process(expected.pid)
    return actual is not None and actual.starttime == expected.starttime


def _group(pgid: int) -> dict[int, _Process]:
    assert pgid > 1
    assert pgid != os.getpgrp()
    result: dict[int, _Process] = {}
    for directory in Path("/proc").iterdir():
        try:
            if not directory.name.isdigit() or directory.stat().st_uid != os.getuid():
                continue
            process = _process(int(directory.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        if process is not None and process.pgid == pgid:
            result[process.pid] = process
    return result


def _cgroup_members(control_group: str) -> tuple[int, ...] | None:
    if not control_group:
        return None
    relative = Path(control_group)
    assert relative.is_absolute()
    assert ".." not in relative.parts
    assert relative.name == _VESSEL
    directory = Path("/sys/fs/cgroup") / control_group.lstrip("/")
    if not directory.exists():
        return ()
    files = tuple(directory.rglob("cgroup.procs"))
    assert len(files) <= 100
    pids = {int(pid) for path in files for pid in path.read_text(encoding="ascii").split()}
    assert len(pids) <= 128
    return tuple(sorted(pids))


@dataclass
class _Manager:
    process: subprocess.Popen[str]
    identity: _Process
    environment: dict[str, str]
    binaries: Mapping[str, Path]
    root: Path
    units: tuple[str, ...]
    outer_group: str
    ancestors: tuple[_Process, ...]
    bus: NativeStopBus
    calls: list[dict[str, object]] = field(default_factory=list)
    deadline: float | None = None
    reference_held: bool = False
    reference_buses: list[MessageBus] = field(default_factory=list)
    lose_reference: bool = False
    loss_event: dict[str, object] | None = None

    def attest(self) -> None:
        assert self.process.poll() is None, "private manager exited"
        assert _same_process(self.identity), "private manager identity changed"
        assert Path(f"/proc/{self.process.pid}/exe").resolve() == self.binaries["systemd"]
        current = _process(self.process.pid)
        assert current is not None
        assert Path(current.cgroup.strip().removeprefix("0::")).is_relative_to(self.outer_group)
        assert self.environment["DBUS_SESSION_BUS_ADDRESS"] == self.bus.address
        assert self.environment["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={self.bus.runtime / 'disabled-system-bus'}"
        runtime = Path(self.environment["XDG_RUNTIME_DIR"])
        metadata = runtime.lstat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        assert metadata.st_uid == os.getuid()
        assert runtime.parent.name == "manager"
        assert runtime.parent.parent.name.startswith("lns-")
        assert stat.S_ISDIR((runtime / "systemd").lstat().st_mode)
        private = runtime / "systemd/private"
        metadata = private.lstat()
        assert stat.S_ISSOCK(metadata.st_mode)
        assert metadata.st_uid == os.getuid()
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.5)
            connection.connect(str(private))
            peer = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        assert peer == (self.process.pid, os.getuid(), os.getgid()), "private bus is not owned by the child manager"

    def run(self, argv: tuple[str, ...], *, timeout_s: float) -> ProcessResult:
        self.attest()
        assert argv[:2] == (str(self.binaries["systemctl"]), "--user")
        assert argv[2] in {"show", "start", "stop", "list-jobs"}
        if argv[2] == "start":
            assert _VESSEL in self.units
            assert argv == (str(self.binaries["systemctl"]), "--user", "start", _VESSEL)
        assert all(
            not argument.endswith(".service")
            or argument in self.units
            or (argv[2] == "show" and argument == _COUNTERPART)
            for argument in argv[3:]
        )
        if self.deadline is not None:
            timeout_s = min(timeout_s, self.deadline - time.monotonic())
        if timeout_s <= 0:
            message = "private controller observation window expired"
            raise ProcessInvocationError(message)
        call: dict[str, object] = {
            "argv": argv,
            "timeout_s": timeout_s,
            "monotonic_ns": time.monotonic_ns(),
            "reference_held": self.reference_held,
        }
        self.calls.append(call)
        try:
            with tempfile.TemporaryFile(dir=self.root) as stdout, tempfile.TemporaryFile(dir=self.root) as stderr:
                completed = subprocess.run(  # noqa: S603 - attested binary and closed private-manager argv
                    argv,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout_s,
                    check=False,
                )
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(_OUTPUT_BYTES + 1), stderr.read(_OUTPUT_BYTES + 1)
            assert len(out) <= _OUTPUT_BYTES, "private systemctl stdout exceeded budget"
            assert len(err) <= _OUTPUT_BYTES, "private systemctl stderr exceeded budget"
        except (OSError, subprocess.SubprocessError) as exc:
            call["error"] = type(exc).__name__
            message = "private systemctl invocation failed or timed out"
            raise ProcessInvocationError(message) from exc
        call.update(returncode=completed.returncode, stdout=out.decode(), stderr=err.decode())
        if "--property=ExecMainCode" in argv:
            assert self.reference_held
            assert len(self.reference_buses) == 1
            reference_bus = self.reference_buses[0]
            assert reference_bus.connected
            fields = dict(line.split("=", 1) for line in out.decode().splitlines())
            call["terminal_fields"] = fields
            call["reference_unique_name"] = reference_bus.unique_name
            call["terminal_capture_ns"] = time.monotonic_ns()
            if self.lose_reference:
                assert fields == _CLEAN_EXIT
                assert self.loss_event is None
                # This shuts down the actual production bus socket. The next
                # production verify must refuse the real lost connection.
                reference_bus.disconnect()
                self.loss_event = {
                    "monotonic_ns": time.monotonic_ns(),
                    "reference_unique_name": reference_bus.unique_name,
                    "connected_after_disconnect": reference_bus.connected,
                }
        return ProcessResult(argv, completed.returncode, out.decode(), err.decode())


@contextmanager
def _control_deadline() -> Generator[None]:
    def expired(signum: int, frame: object) -> None:
        del signum, frame
        message = "native controller exceeded its declared 45-second outer window"
        raise TimeoutError(message)

    previous = signal.signal(signal.SIGALRM, expired)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    signal.setitimer(signal.ITIMER_REAL, _CONTROL_OUTER_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast("int", listener.getsockname()[1])


def _listener_absent(port: int) -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.2)
        result = connection.connect_ex(("127.0.0.1", port))
    assert result in {0, errno.ECONNREFUSED}, f"listener absence is unknown: {result}"
    return result == errno.ECONNREFUSED


def _private_listener_absent(runtime: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(0.2)
        result = connection.connect_ex(str(runtime / "systemd/private"))
    assert result in {0, errno.ECONNREFUSED, errno.ENOENT}, f"private listener absence is unknown: {result}"
    return result != 0


def _ready(manager: _Manager, port: int, token: str) -> None:
    deadline = time.monotonic() + _LISTENER_SECONDS
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=0.5) as client:
        while time.monotonic() < deadline:
            manager.attest()
            try:
                reply = client.get(f"http://127.0.0.1:{port}/ready")
            except httpx.TransportError:
                time.sleep(0.05)
                continue
            assert reply.status_code == 200, "unready listener"
            assert reply.json() == {"marker": token}, "foreign listener"
            return
    pytest.fail("synthetic native listener did not become ready in 10 seconds")


def _inventory(scribe: ScribeService, manager: _Manager) -> OperatorInventoryService:
    paths = OperatorPaths(
        *(
            manager.root / name
            for name in ("codex", "config", "receipt", "runes", "quadlet", "generated", "storage", "snapshots")
        )
    )
    return OperatorInventoryService(
        paths=paths,
        receipt=LifecycleReceiptStore(paths.receipt),
        units=OwnedUnitInventoryService(scribe, manager, systemctl_bin=str(manager.binaries["systemctl"])),
        storage=StorageInventoryService(manager, findmnt_bin=None),
        animators=ConfiguredAnimatorDeclarations(runes_dir=paths.runes),
    )


def _prepare(
    root: Path,
    case: str,
    token: str,
    port: int,
    binaries: Mapping[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    scavenger_profile: Path | None = None,
) -> tuple[ScribeService, dict[str, str], tuple[str, ...], dict[str, object], NativeStopBus]:
    generated, quadlet, harness = (root / name for name in ("generated", "quadlet", "harness"))
    installation = root / "installation"
    for directory in (generated, quadlet, harness, installation / "bin"):
        directory.mkdir(parents=True, mode=0o700)
    credential = root / "credential"
    with credential.open("x", encoding="ascii") as stream:
        credential.chmod(0o600)
        stream.write(secrets.token_urlsafe(32))
    events = root / "events.jsonl"
    events.touch(mode=0o600)
    config = root / "fixture.json"
    _write_json(
        config,
        {
            "case": "clean" if case == "reference-loss" else case,
            "token": token,
            "port": port,
            "credential_path": str(credential),
            "events_path": str(events),
        },
    )
    if scavenger_profile is not None:
        _write_json(config, {"profile_path": str(scavenger_profile), "port": port})
    fixture = _SCAVENGER_FIXTURE if scavenger_profile is not None else _FIXTURE
    shim = installation / "bin/lychd"
    shim.write_text(
        "\n".join(
            (
                f"#!{sys.executable} -B",
                "import importlib.util, json, os, resource, runpy, stat, sys",
                "from pathlib import Path",
                "sys.dont_write_bytecode = True",
                "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'",
                f"source = Path({str(_REPOSITORY / 'src')!r}).resolve(strict=True)",
                "assert 'lychd' not in sys.modules",
                "sys.path.insert(0, str(source))",
                "spec = importlib.util.find_spec('lychd')",
                "assert spec is not None and spec.origin is not None",
                "assert Path(spec.origin).resolve(strict=True) == source / 'lychd/__init__.py'",
                "assert tuple(spec.submodule_search_locations or ()) == (str(source / 'lychd'),)",
                "log_path = os.environ.get('LYCHD_NATIVE_PROOF_LOG')",
                "if log_path:",
                "    metadata = os.lstat(log_path)",
                "    assert stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()",
                "    assert stat.S_IMODE(metadata.st_mode) == 0o600",
                "    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)",
                "    actual = os.fstat(fd)",
                "    assert (actual.st_dev, actual.st_ino) == (metadata.st_dev, metadata.st_ino)",
                "    os.dup2(fd, 1)",
                "    os.dup2(fd, 2)",
                "    os.close(fd)",
                f"    resource.setrlimit(resource.RLIMIT_FSIZE, ({_NATIVE_LOG_BYTES}, {_NATIVE_LOG_BYTES}))",
                "def event(name):",
                "    if log_path:",
                "        value = {'event': name, 'pid': os.getpid(), 'invocation_id': os.environ['INVOCATION_ID'], 'source_root': str(source), 'package_origin': str(Path(spec.origin).resolve(strict=True)), 'dont_write_bytecode': sys.dont_write_bytecode, 'bytecode_flag': bool(sys.flags.dont_write_bytecode), 'unbuffered': bool(getattr(sys.stdout, 'write_through', False))}",
                "        payload = b'LYCHD_NATIVE_PROOF ' + json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\\n'",
                "        assert os.write(1, payload) == len(payload)",
                "event('boot-start')",
                "try:",
                f"    runpy.run_path({fixture.as_posix()!r}, run_name='__main__', init_globals={{'CONFIG_PATH': {str(config)!r}, 'SOURCE_ROOT': str(source)}})",
                "finally:",
                "    primary = sys.exc_info()[0]",
                "    try:",
                "        sys.stdout.flush()",
                "        sys.stderr.flush()",
                "        event('boot-end')",
                "    except BaseException:",
                "        if primary is None:",
                "            raise",
                "",
            )
        ),
        encoding="utf-8",
    )
    shim.chmod(0o700)
    marker_script = root / "marker.py"
    marker_script.write_text(
        "import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text(str(time.monotonic_ns()), encoding='ascii')\n",
        encoding="utf-8",
    )
    settings = SettingsSnapshot(
        payload=json.dumps({"server": {"port": port, "database": {"profile": "memory"}}})
    ).materialize()
    with monkeypatch.context() as patch:
        # Substitute the compiler's installation, never the process-global sys.prefix.
        patch.setattr(bind_compilation, "sys", SimpleNamespace(prefix=str(installation)))
        native = bind_compilation._compile_uncaged_vessel_unit(settings=settings)  # pyright: ignore[reportPrivateUsage]
    native = native.model_copy(
        update={
            "environment": {
                **native.environment,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        }
    )
    if scavenger_profile is not None:
        log_path = root / "native-output.log"
        log_path.touch(mode=0o600, exist_ok=False)
        native = native.model_copy(
            update={
                "environment": {
                    **native.environment,
                    "LYCHD_NATIVE_PROOF_LOG": str(log_path),
                }
            }
        )
    assert native.filename == _VESSEL
    assert native.exec_start == f"{systemd_executable_token(shim)} serve --host 127.0.0.1 --port {port}"
    dependencies = (
        ()
        if scavenger_profile is not None
        else tuple(f"lychd-proof-{token}-dependency-{name}.service" for name in ("a", "b"))
    )
    plain_units = {_VESSEL: native.render()}
    for unit in dependencies:
        marker = root / f"{unit}.stopped"
        plain_units[unit] = "\n".join(
            (
                "[Unit]",
                "Description=LychD inert dependency sentinel",
                "[Service]",
                "Type=oneshot",
                f"ExecStart={binaries['true']}",
                f"ExecStop={systemd_executable_token(Path(sys.executable))} {systemd_executable_token(marker_script)} {systemd_executable_token(marker)}",
                "RemainAfterExit=yes",
                "",
            )
        )
    for unit in ("basic.target", "shutdown.target"):
        (harness / unit).write_text(
            "[Unit]\nDescription=LychD private native proof support\nDefaultDependencies=no\n", encoding="utf-8"
        )
    if case == "clean" and scavenger_profile is None:
        (harness / _COUNTERPART).write_text(
            "[Unit]\nDescription=LychD foreign counterpart sentinel\n"
            f"[Service]\nType=oneshot\nExecStart={binaries['true']}\nRemainAfterExit=yes\n",
            encoding="utf-8",
        )
    scribe = ScribeService(output_dir=quadlet, systemd_dir=generated)
    scribe.reconcile_all((), plain_units=plain_units)
    manager_storage = Path(tempfile.mkdtemp(prefix="lns-"))
    environment = manager_environment(manager_storage / "manager", generated, harness, inherit_environment=False)
    bus = prepare_bus(
        runtime=Path(environment["XDG_RUNTIME_DIR"]), harness=harness, root=root, binaries=broker_binaries()
    )
    environment["DBUS_SESSION_BUS_ADDRESS"] = bus.address
    environment["DBUS_SYSTEM_BUS_ADDRESS"] = f"unix:path={bus.runtime / 'disabled-system-bus'}"
    metadata: dict[str, object] = {
        "generated_sha256": {name: _digest(generated / name) for name in plain_units},
        "shim_sha256": _digest(shim),
        "marker_sha256": _digest(marker_script),
        "fixture_config_sha256": _digest(config),
        "installation_substitution": str(installation),
        "generated_exec_start": native.exec_start,
        "credential_mode": "0600; synthetic value excluded from argv/environment/receipt",
    }
    return scribe, environment, dependencies, metadata, bus


def _capture_native(snapshot: Mapping[str, str], owned: dict[int, _Process], outer_group: str) -> _Process:
    native = _process(int(snapshot["MainPID"]))
    assert native is not None
    assert native.pid > 1
    assert native.pgid != os.getpgrp()
    assert Path(native.cgroup.strip().removeprefix("0::")).is_relative_to(outer_group)
    assert Path(snapshot["ControlGroup"]).is_relative_to(outer_group)
    assert snapshot["ControlGroup"] != outer_group
    owned.update(_group(native.pgid))
    members = _cgroup_members(snapshot["ControlGroup"])
    if members is not None:
        assert native.pid in members, "manager native cgroup does not contain its MainPID"
        for pid in members:
            process = _process(pid)
            assert process is not None
            owned[pid] = process
    return native


def _outer_descendants(manager: _Manager) -> dict[int, _Process]:
    directory = Path("/sys/fs/cgroup") / manager.outer_group.lstrip("/")
    assert directory.is_dir(), "outer proof cgroup disappeared during owned cleanup"
    files = tuple(directory.rglob("cgroup.procs"))
    assert len(files) <= 100
    pids = {int(pid) for path in files for pid in path.read_text(encoding="ascii").split()}
    assert len(pids) <= 128
    baseline = {process.pid: process.starttime for process in manager.ancestors}
    descendants: dict[int, _Process] = {}
    for pid in pids:
        process = _process(pid)
        if process is not None and baseline.get(pid) != process.starttime:
            assert Path(process.cgroup.strip().removeprefix("0::")).is_relative_to(manager.outer_group)
            descendants[pid] = process
    return descendants


def _cleanup(
    manager: _Manager,
    port: int,
    owned: dict[int, _Process],
    native: _Process | None,
    control_group: str,
    receipt: dict[str, Any],
) -> None:
    cleanup: dict[str, object] = {}
    receipt["cleanup"] = cleanup
    try:
        # The pre-launch outer baseline pins the caller and every ancestor by
        # PID/starttime. Capture all later occurrences in the exclusive subtree
        # even when startup failed before native MainPID or bus observation.
        owned.update(_outer_descendants(manager))
        if manager.process.poll() is None:
            manager.attest()
            snapshots = read_properties(
                systemctl=manager.binaries["systemctl"], environment=manager.environment, units=manager.units
            )
            current = snapshots[_VESSEL]
            if int(current["MainPID"]):
                native = _capture_native(current, owned, manager.outer_group)
                control_group = current["ControlGroup"]
            stopped = run_systemctl(
                manager.binaries["systemctl"],
                manager.environment,
                "stop",
                "--job-mode=fail",
                *manager.units,
                timeout=5.0,
            )
            cleanup["exact_stop_returncode"] = stopped.returncode
            manager.attest()
            bus_stopped = run_systemctl(
                manager.binaries["systemctl"],
                manager.environment,
                "stop",
                "--job-mode=fail",
                *manager.bus.units,
                timeout=5.0,
            )
            cleanup["bus_stop_returncode"] = bus_stopped.returncode
    except (AssertionError, OSError, subprocess.SubprocessError) as exc:
        cleanup["manager_stop_error"] = type(exc).__name__
    finally:
        stop_private_manager(manager.process, Path(manager.environment["XDG_RUNTIME_DIR"]))
    cleanup["manager_exit"] = manager.process.returncode
    owned.update(_outer_descendants(manager))
    # Kill only exact captured occurrences, after rechecking PID reuse. Never
    # signal a shared group, broad cgroup or the installed user manager.
    forced: list[dict[str, object]] = []
    for process in owned.values():
        if _same_process(process):
            forced.append(asdict(process))
            with suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)
    cleanup["forced_pid_occurrences"] = forced
    deadline = time.monotonic() + _OBSERVATION_SECONDS
    while True:
        live = [pid for pid, process in owned.items() if _same_process(process)]
        group = _group(native.pgid) if native is not None else {}
        members = _cgroup_members(control_group)
        descendants = _outer_descendants(manager)
        absent = _listener_absent(port)
        bus_absent = manager.bus.listener_absent()
        private_absent = _private_listener_absent(manager.bus.runtime)
        if not live and not group and not members and not descendants and absent and bus_absent and private_absent:
            cleanup.update(
                captured_processes_absent=True,
                native_group_absent=True,
                native_cgroup_members=members,
                listener_absent=True,
                outer_descendants_absent=True,
                full_bus_listener_absent=True,
                private_peer_listener_absent=True,
            )
            break
        if time.monotonic() >= deadline:
            cleanup.update(
                captured_processes_absent=not live,
                native_group_survivors=list(group),
                native_cgroup_members=members,
                listener_absent=absent,
                outer_descendants_absent=not descendants,
                full_bus_listener_absent=bus_absent,
                private_peer_listener_absent=private_absent,
            )
            break
        time.sleep(0.05)
    cleanup["captured_processes"] = [asdict(process) for process in owned.values()]
    cleanup["success"] = (
        cleanup.get("captured_processes_absent") is True
        and cleanup.get("native_group_absent") is True
        and cleanup.get("listener_absent") is True
        and cleanup.get("outer_descendants_absent") is True
        and cleanup.get("full_bus_listener_absent") is True
        and cleanup.get("private_peer_listener_absent") is True
    )
    assert cleanup["success"], f"owned native cleanup is unproved; inspect {manager.root / 'receipt.json'}"


def _assert_outcome(
    *,
    manager: _Manager,
    root: Path,
    case: str,
    dependencies: tuple[str, ...],
    after: dict[str, dict[str, str]],
    error: OperatorError | None,
    receipt: dict[str, Any],
) -> None:
    stops = [
        tuple(cast("tuple[str, ...]", call["argv"])[4:])
        for call in manager.calls
        if cast("tuple[str, ...]", call["argv"])[2] == "stop"
    ]
    events = [
        cast("dict[str, Any]", json.loads(line))
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    receipt["events"] = events
    names = [event["event"] for event in events]
    assert names[:3] == ["fixture-cli-argv-accepted", "fixture-startup-enter", "fixture-startup-complete"]
    assert names.count("fixture-close-enter") == 1
    assert after[_VESSEL]["MainPID"] == "0"
    assert after[_VESSEL]["ControlPID"] == "0"
    captured = [call for call in manager.calls if "terminal_fields" in call]
    assert len(captured) == 1
    terminal = cast("dict[str, str]", captured[0]["terminal_fields"])
    assert captured[0]["reference_held"] is True
    if case == "close-failure":
        assert error is not None
        assert stops == [(_VESSEL,)]
        assert after[_VESSEL]["Result"] == "exit-code"
        assert after[_VESSEL]["ExecMainStatus"] == "1"
        assert terminal["ExecMainCode"] == "1"
        assert terminal["ExecMainStatus"] == "1"
        assert terminal["Result"] == "exit-code"
        assert "fixture-required-close-failure" in names
        assert "fixture-close-complete" not in names
        assert all(after[unit]["ActiveState"] == "active" for unit in dependencies)
        assert not any((root / f"{unit}.stopped").exists() for unit in dependencies)
    elif case == "clean":
        assert error is None
        assert stops == [(_VESSEL,), tuple(sorted(dependencies))]
        assert (
            after[_VESSEL]["ActiveState"],
            after[_VESSEL]["SubState"],
        ) == ("inactive", "dead")
        assert terminal == _CLEAN_EXIT
        closed = next(event["monotonic_ns"] for event in events if event["event"] == "fixture-close-complete")
        markers = {unit: int((root / f"{unit}.stopped").read_text(encoding="ascii")) for unit in dependencies}
        receipt["dependency_stop_markers"] = markers
        assert all(stopped > closed for stopped in markers.values())
        assert all(snapshot["ActiveState"] == "inactive" for snapshot in after.values())
    else:
        assert case == "reference-loss"
        assert terminal == _CLEAN_EXIT
        assert manager.loss_event is not None
        assert manager.loss_event["connected_after_disconnect"] is False
        assert cast("int", manager.loss_event["monotonic_ns"]) > cast("int", captured[0]["terminal_capture_ns"])
        assert error is not None
        assert stops == [(_VESSEL,)]
        assert all(after[unit]["ActiveState"] == "active" for unit in dependencies)
        assert not any((root / f"{unit}.stopped").exists() for unit in dependencies)
        assert "fixture-close-complete" in names


def _native_inputs(scribe: ScribeService, root: Path) -> dict[str, object]:
    generation = scribe.inspect_owned_bindings().generation
    assert generation
    paths = (root / "generated" / _VESSEL, root / "installation/bin/lychd", root / "fixture.json")
    return {
        "generation": generation,
        "startup_sha256": {str(path): _digest(path) for path in paths},
        "source_sha256": {name: _digest(_REPOSITORY / name) for name in _SOURCES},
    }


def _require_native_inputs(scribe: ScribeService, root: Path, pinned: dict[str, object]) -> None:
    assert _native_inputs(scribe, root) == pinned, "native binding/startup/source inputs changed"


def _retain_native_log(root: Path, receipt: dict[str, Any]) -> bytes:
    path = root / "native-output.log"
    record: dict[str, object] = {"path": str(path), "limit_bytes": _NATIVE_LOG_BYTES, "complete": False}
    receipt["native_log"] = record
    try:
        metadata = path.lstat()
        record.update(
            bytes=metadata.st_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=oct(stat.S_IMODE(metadata.st_mode)),
        )
        assert stat.S_ISREG(metadata.st_mode)
        assert metadata.st_uid == os.getuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        with path.open("rb") as stream:
            content = stream.read(_NATIVE_LOG_BYTES + 1)
        record["saturated"] = len(content) >= _NATIVE_LOG_BYTES
        record["complete"] = len(content) == metadata.st_size and len(content) < _NATIVE_LOG_BYTES
        assert record["complete"] is True, "native output capture saturated or changed; complete output unproved"
        record["sha256"] = hashlib.sha256(content).hexdigest()
    except BaseException as exc:
        record["capture_error"] = type(exc).__name__
        receipt["proof_success"] = False
        raise
    else:
        return content


def _record_native_boot(
    root: Path,
    receipt: dict[str, Any],
    native: _Process,
    invocation_id: str,
) -> None:
    content = _retain_native_log(root, receipt)
    records = [
        cast("dict[str, Any]", json.loads(line.removeprefix(_NATIVE_LOG_PREFIX)))
        for line in content.splitlines()
        if line.startswith(_NATIVE_LOG_PREFIX)
    ]
    boots = cast("list[dict[str, Any]]", receipt.setdefault("native_boots", []))
    assert len(records) == (len(boots) + 1) * 3
    current = records[-3:]
    assert [record["event"] for record in current] == ["boot-start", "source-loaded", "boot-end"]
    for record in current:
        assert type(record["pid"]) is int
        assert record["pid"] == native.pid
        assert record["invocation_id"] == invocation_id
        assert record["source_root"] == str((_REPOSITORY / "src").resolve(strict=True))
        assert record["package_origin"] == str((_REPOSITORY / "src/lychd/__init__.py").resolve(strict=True))
        assert record["dont_write_bytecode"] is True
        assert record["bytecode_flag"] is True
        assert record["unbuffered"] is True
    assert current[1]["entrypoint_origin"] == str((_REPOSITORY / "src/lychd/__main__.py").resolve(strict=True))
    previous_end = 0
    if boots:
        previous_end = cast("int", boots[-1]["log_end_offset"])
        assert hashlib.sha256(content[:previous_end]).hexdigest() == boots[-1]["log_prefix_sha256"]
        assert receipt["native_log"]["inode"] == boots[-1]["log_inode"]
        assert receipt["native_log"]["device"] == boots[-1]["log_device"]
    boots.append(
        {
            "process": asdict(native),
            "InvocationID": invocation_id,
            "source_events": current,
            "log_start_offset": previous_end,
            "log_end_offset": len(content),
            "log_prefix_sha256": hashlib.sha256(content).hexdigest(),
            "log_inode": receipt["native_log"]["inode"],
            "log_device": receipt["native_log"]["device"],
        }
    )


def _stop_native(
    manager: _Manager,
    scribe: ScribeService,
    access: LocalAccess,
    monkeypatch: pytest.MonkeyPatch,
    receipt: dict[str, Any],
    *,
    expected_generation: str | None = None,
) -> OperatorError | None:
    """Reuse one signed owned stop, retaining exact exit evidence through settlement."""
    assert all(not bus.connected for bus in manager.reference_buses)
    manager.reference_buses.clear()
    manager.calls = []
    root, binaries, bus = manager.root, manager.binaries, manager.bus
    inventory = _inventory(scribe, manager)
    client = StopAdmissionClient(access_loader=lambda: access)
    admissions: list[str] = []

    def admit(generation: str) -> None:
        assert expected_generation is None or generation == expected_generation
        client.admit(generation)
        admissions.append(generation)

    reference_events: list[dict[str, object]] = []

    @contextmanager
    def hold_reference() -> Generator[stop_evidence.ExitReference]:
        with stop_evidence.hold_native_exit(runtime_dir=bus.runtime) as reference:
            manager.reference_held = True
            reference_events.append({"event": "reference-held", "monotonic_ns": time.monotonic_ns()})
            try:
                yield reference
            finally:
                manager.reference_held = False
                reference_events.append({"event": "reference-scope-exit", "monotonic_ns": time.monotonic_ns()})

    new_bus = stop_evidence._new_bus  # pyright: ignore[reportPrivateUsage]

    def capture_reference_bus(address: str) -> Any:
        reference_bus = new_bus(address)
        manager.reference_buses.append(reference_bus)
        return reference_bus

    control = OperatorControlService(
        inventory=inventory,
        targets=OperatorTargetResolver(inventory),
        runner=manager,
        systemctl_bin=str(binaries["systemctl"]),
        lock_factory=lambda: LifecycleLock(root / "lifecycle.lock"),
        stop_admission=admit,
        exit_reference_factory=hold_reference,
    )
    error: OperatorError | None = None
    manager.deadline = time.monotonic() + _CONTROL_OUTER_SECONDS
    with monkeypatch.context() as patch, _control_deadline():
        patch.setattr(stop_evidence, "_new_bus", capture_reference_bus)
        try:
            result = control.execute(OperatorAction.STOP, OperatorTarget.SYSTEM)
            assert result.authority is VesselAuthority.VESSEL
        except OperatorError as exc:
            error = exc
    manager.deadline = None
    receipt["controller_calls"] = manager.calls
    receipt["admissions"] = admissions
    receipt["controller_error"] = str(error) if error else None
    receipt["reference_events"] = reference_events
    receipt["reference_loss"] = manager.loss_event
    assert len(manager.reference_buses) == 1
    reference_bus = manager.reference_buses[0]
    assert not reference_bus.connected
    reference_socket = reference_bus._sock  # pyright: ignore[reportPrivateUsage]
    assert reference_socket is None or reference_socket.fileno() == -1
    assert len(admissions) == 1
    assert admissions[0] == scribe.inspect_owned_bindings().generation
    return error


def _counterpart_snapshot(snapshot: Mapping[str, str]) -> dict[str, str | tuple[str, ...]]:
    """Compare every property while treating dependency names as unordered relations."""
    relations = {
        "Requires",
        "RequiredBy",
        "BindsTo",
        "BoundBy",
        "Before",
        "After",
        "Conflicts",
        "ConflictedBy",
        "PartOf",
    }
    return {key: tuple(sorted(value.split())) if key in relations else value for key, value in snapshot.items()}


def _assert_counterpart_call(calls: list[dict[str, object]], *, active: bool) -> None:
    observations = [call for call in calls if _COUNTERPART in cast("tuple[str, ...]", call["argv"])]
    assert len(observations) == 1
    call = observations[0]
    argv = cast("tuple[str, ...]", call["argv"])
    assert argv[2:4] == ("show", _COUNTERPART)
    assert len(argv[4:]) == len(_COUNTERPART_PROPERTIES)
    assert set(argv[4:]) == {f"--property={name}" for name in _COUNTERPART_PROPERTIES}
    assert call["timeout_s"] == 3.0
    assert call["returncode"] == 0
    fields = dict(line.split("=", 1) for line in cast("str", call["stdout"]).splitlines())
    assert fields == {
        "Id": _COUNTERPART,
        "LoadState": "loaded",
        "ActiveState": "active" if active else "inactive",
        "SubState": "exited" if active else "dead",
        "MainPID": "0",
        "ControlPID": "0",
        "Job": "",
    }


def _start_native_with_counterpart(manager: _Manager, scribe: ScribeService, receipt: dict[str, Any]) -> None:
    """Prove real public refusal and native-only bootstrap inside the existing clean case."""
    inventory = _inventory(scribe, manager)
    catalog = inventory.owned_units()
    assert not catalog.warning
    assert catalog.generation
    assert tuple(unit.name for unit in catalog.units) == tuple(sorted(manager.units))
    assert all(unit.state is ObservationState.INACTIVE for unit in catalog.units)
    assert all(unit.sources == (str(manager.root / "generated" / unit.name),) for unit in catalog.units)
    bindings = scribe.inspect_owned_bindings()
    assert _COUNTERPART not in bindings.runtime_units
    assert {source.name for source in bindings.systemd_sources} == set(manager.units)
    record: dict[str, Any] = {"generation": catalog.generation, "owned_units": [asdict(unit) for unit in catalog.units]}
    receipt["public_start"] = record
    control = OperatorControlService(
        inventory=inventory,
        targets=OperatorTargetResolver(inventory),
        runner=manager,
        systemctl_bin=str(manager.binaries["systemctl"]),
        lock_factory=lambda: LifecycleLock(manager.root / "lifecycle.lock"),
    )
    manager.attest()
    foreign_start = run_systemctl(
        manager.binaries["systemctl"], manager.environment, "start", "--job-mode=fail", _COUNTERPART, timeout=5.0
    )
    record["harness_counterpart_start"] = {"argv": foreign_start.args, "returncode": foreign_start.returncode}
    assert foreign_start.returncode == 0, foreign_start.stderr
    active = read_properties(
        systemctl=manager.binaries["systemctl"], environment=manager.environment, units=(_COUNTERPART,)
    )
    record["counterpart_active"] = active
    assert active[_COUNTERPART]["FragmentPath"] == str(manager.root / "harness" / _COUNTERPART)
    assert active[_COUNTERPART]["InvocationID"]
    assert active[_COUNTERPART]["ActiveState"] == "active"
    manager.calls = []
    record["refused_calls"] = manager.calls
    with _control_deadline(), pytest.raises(OperatorAuthorityError) as refused:
        control.execute(OperatorAction.START, OperatorTarget.SYSTEM)
    record["refusal"] = str(refused.value)
    assert all(cast("tuple[str, ...]", call["argv"])[2] == "show" for call in manager.calls)
    _assert_counterpart_call(manager.calls, active=True)
    unchanged = read_properties(
        systemctl=manager.binaries["systemctl"], environment=manager.environment, units=(*manager.units, _COUNTERPART)
    )
    record["after_refusal"] = unchanged
    assert _counterpart_snapshot(unchanged[_COUNTERPART]) == _counterpart_snapshot(active[_COUNTERPART])
    assert all(unchanged[unit]["ActiveState"] == "inactive" for unit in manager.units)
    assert scribe.inspect_owned_bindings() == bindings
    _resume_native_after_counterpart(manager, scribe, control, record)
    assert scribe.inspect_owned_bindings() == bindings


def _resume_native_after_counterpart(
    manager: _Manager,
    scribe: ScribeService,
    control: OperatorControlService,
    record: dict[str, Any],
) -> None:
    manager.attest()
    foreign_stop = run_systemctl(
        manager.binaries["systemctl"], manager.environment, "stop", "--job-mode=fail", _COUNTERPART, timeout=5.0
    )
    record["harness_counterpart_stop"] = {"argv": foreign_stop.args, "returncode": foreign_stop.returncode}
    assert foreign_stop.returncode == 0, foreign_stop.stderr
    inactive = read_properties(
        systemctl=manager.binaries["systemctl"], environment=manager.environment, units=(_COUNTERPART,)
    )
    record["counterpart_inactive"] = inactive
    assert inactive[_COUNTERPART]["ActiveState"] == "inactive"
    manager.calls = []
    record["accepted_calls"] = manager.calls
    with _control_deadline():
        result = control.execute(OperatorAction.START, OperatorTarget.SYSTEM)
    assert result.authority is VesselAuthority.DIRECT
    assert result.units == (_VESSEL,)
    _assert_counterpart_call(manager.calls, active=False)
    effects = [
        cast("tuple[str, ...]", call["argv"])
        for call in manager.calls
        if cast("tuple[str, ...]", call["argv"])[2] != "show"
    ]
    assert effects == [(str(manager.binaries["systemctl"]), "--user", "start", _VESSEL)]
    assert cast("tuple[str, ...]", manager.calls[-2]["argv"])[2:4] == ("show", _COUNTERPART)
    assert cast("tuple[str, ...]", manager.calls[-1]["argv"])[2:] == ("start", _VESSEL)
    settled = read_properties(
        systemctl=manager.binaries["systemctl"], environment=manager.environment, units=(*manager.units, _COUNTERPART)
    )
    record["after_start"] = settled
    assert _counterpart_snapshot(settled[_COUNTERPART]) == _counterpart_snapshot(inactive[_COUNTERPART])
    assert settled[_VESSEL]["ActiveState"] == "active"
    assert all(settled[unit]["ActiveState"] == "inactive" for unit in manager.units if unit != _VESSEL)
    assert scribe.inspect_owned_bindings().generation == record["generation"]


def _ready_retained(manager: _Manager, retained: RetainedScavenger) -> None:
    deadline = time.monotonic() + _LISTENER_SECONDS
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=0.5) as client:
        while time.monotonic() < deadline:
            manager.attest()
            try:
                document = retained.document(client)
            except httpx.TransportError:
                time.sleep(0.05)
                continue
            _write_json(manager.root / f"canonical-{time.monotonic_ns()}.json", document)
            return
    pytest.fail("production retained Scavenger did not publish in the existing listener window")


def _assert_retained_stop(manager: _Manager, error: OperatorError | None) -> None:
    assert error is None
    stops = [
        tuple(cast("tuple[str, ...]", call["argv"])[4:])
        for call in manager.calls
        if cast("tuple[str, ...]", call["argv"])[2] == "stop"
    ]
    assert stops == [(_VESSEL,)]
    captured = [call for call in manager.calls if "terminal_fields" in call]
    assert len(captured) == 1
    assert captured[0]["terminal_fields"] == _CLEAN_EXIT
    assert captured[0]["reference_held"] is True


def _case(  # noqa: PLR0915, PLR0912, C901 - finite proof and cleanup stay explicit
    root: Path,
    case: str,
    binaries: Mapping[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    scavenger_profile: Path | None = None,
) -> None:
    root.mkdir(mode=0o700)
    token, port = uuid.uuid4().hex, _free_port()
    scribe, environment, dependencies, metadata, bus = _prepare(
        root, case, token, port, binaries, monkeypatch, scavenger_profile=scavenger_profile
    )
    units = (_VESSEL, *dependencies)
    pinned_inputs = _native_inputs(scribe, root) if scavenger_profile is not None else None
    pinned_generation = cast("str", pinned_inputs["generation"]) if pinned_inputs is not None else None
    receipt: dict[str, Any] = {
        "case": case,
        "token": token,
        "port": port,
        "units": units,
        "source_sha256": {name: _digest(_REPOSITORY / name) for name in _SOURCES},
        "dependencies": {
            name: importlib.metadata.version(name)
            for name in ("litestar", "granian", "httpx", "http-message-signatures", "dbus-fast")
        },
        "python": {"version": sys.version, "executable": sys.executable, "sha256": _digest(Path(sys.executable))},
        "binaries": {name: {"path": str(path), "sha256": _digest(path)} for name, path in binaries.items()},
        "environment": environment,
        "budgets_seconds": {
            "manager_ready": 5,
            "bus_registration": 5,
            "start": 5,
            "listener_ready": _LISTENER_SECONDS,
            "controller_process": 30,
            "controller_outer": _CONTROL_OUTER_SECONDS,
            "observation": _OBSERVATION_SECONDS,
            "cleanup_stop": 5,
            "manager_terminate": 2,
            "manager_kill": 2,
            "cleanup_observation": _OBSERVATION_SECONDS,
        },
        **metadata,
        "proof_success": False,
        "pinned_native_inputs": pinned_inputs,
    }
    manager: _Manager | None = None
    child: subprocess.Popen[str] | None = None
    native: _Process | None = None
    owned: dict[int, _Process] = {}
    control_group = ""
    retained: RetainedScavenger | None = None
    with (root / "manager.log").open("w", encoding="utf-8") as log:
        try:
            outer = require_delegated_manager_cgroup(binaries["systemctl"])
            receipt["outer_service"] = outer
            ancestors = tuple(_process(int(pid)) for pid in outer["AncestorPids"].split())
            assert all(process is not None for process in ancestors)
            baseline = cast("tuple[_Process, ...]", ancestors)
            receipt["outer_baseline_processes"] = [asdict(process) for process in baseline]
            child = subprocess.Popen(  # noqa: S603 - root-controlled manager, closed options and private environment
                [
                    str(binaries["systemd"]),
                    "--user",
                    "--unit=basic.target",
                    "--log-target=console",
                    "--log-level=warning",
                    "--no-pager",
                ],
                env=environment,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            identity = _process(child.pid)
            assert identity is not None
            manager = _Manager(
                child, identity, environment, binaries, root, units, outer["ControlGroup"], baseline, bus
            )
            manager.lose_reference = case == "reference-loss"
            receipt["manager_identity"] = asdict(identity)
            version = subprocess.run(  # noqa: S603 - attested binary and literal read-only argument
                [str(binaries["systemd"]), "--version"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=3.0,
            )
            receipt["manager_version"] = version.stdout
            unavailable = wait_for_manager(child, systemctl=binaries["systemctl"], environment=environment, timeout=5.0)
            if unavailable:
                proof_unavailable(f"isolated native-stop manager unavailable: {unavailable}")
            manager.attest()
            private = Path(environment["XDG_RUNTIME_DIR"]) / "systemd/private"
            receipt["private_socket"] = {
                "path": str(private),
                "inode": private.lstat().st_ino,
                "uid": private.lstat().st_uid,
            }
            manager.attest()
            bus_start = run_systemctl(
                binaries["systemctl"], environment, "start", "--job-mode=fail", *bus.units, timeout=5.0
            )
            assert bus_start.returncode == 0, bus_start.stderr
            bus_snapshot = bus.attest(
                manager_pid=child.pid,
                outer_group=outer["ControlGroup"],
                systemctl=binaries["systemctl"],
                environment=environment,
            )
            receipt["full_bus"] = bus_snapshot
            if scavenger_profile is not None:
                # This is the first access to the external profile or any credential/DB.
                manager.attest()
                retained = RetainedScavenger.load(scavenger_profile, port=port)
                _write_json(root / "accounting-before-start.json", retained.accounting())
                receipt["qualification"] = {
                    "production_idle_publication_and_native_vessel_stop_restart": True,
                    "postgres_external_preserved": True,
                    "full_native_log_review": "required trusted Root acceptance; not performed by this test",
                    "public_start_installed_lifetime_and_dependency_shutdown": False,
                }
            for pid in bus_snapshot["process_pids"]:
                process = _process(pid)
                assert process is not None
                owned[pid] = process
            if pinned_inputs is not None:
                _require_native_inputs(scribe, root, pinned_inputs)
            if case == "clean" and scavenger_profile is None:
                _start_native_with_counterpart(manager, scribe, receipt)
                start_units = dependencies
            else:
                start_units = (*dependencies, _VESSEL)
            started = run_systemctl(
                binaries["systemctl"], environment, "start", "--job-mode=fail", *start_units, timeout=5.0
            )
            assert started.returncode == 0, started.stderr
            if retained is None:
                _ready(manager, port, token)
            else:
                _ready_retained(manager, retained)
                _write_json(root / "accounting-first-start.json", retained.accounting())
            before = read_properties(systemctl=binaries["systemctl"], environment=environment, units=units)
            receipt["before"] = before
            assert all(snapshot["ActiveState"] == "active" for snapshot in before.values())
            for unit, snapshot in before.items():
                assert snapshot["FragmentPath"] == str(root / "generated" / unit)
                if snapshot["ControlGroup"]:
                    assert Path(snapshot["ControlGroup"]).is_relative_to(outer["ControlGroup"])
                    assert snapshot["ControlGroup"] != outer["ControlGroup"]
            assert before[_VESSEL]["FragmentPath"] == str(root / "generated" / _VESSEL)
            assert before[_VESSEL]["InvocationID"]
            assert before[_VESSEL]["Restart"] == "on-failure"
            assert before[_VESSEL]["RequiredBy"] == ""
            assert before[_VESSEL]["BoundBy"] == ""
            incoming = run_systemctl(binaries["systemctl"], environment, "show", _VESSEL, "--property=WantedBy")
            assert incoming.returncode == 0
            assert incoming.stdout == "WantedBy=\n"
            receipt["native_incoming_wants"] = incoming.stdout
            assert str(root / "installation/bin/lychd") in before[_VESSEL]["ExecStart"]
            native = _capture_native(before[_VESSEL], owned, outer["ControlGroup"])
            control_group = before[_VESSEL]["ControlGroup"]
            receipt["native_identity"] = asdict(native)
            receipt["effective_defaults"] = {
                name: before[_VESSEL][name]
                for name in ("TimeoutStopUSec", "KillMode", "KillSignal", "SendSIGKILL", "SuccessExitStatus", "Restart")
            }
            access = (
                retained.access()
                if retained is not None
                else LocalAccess(
                    url=f"http://127.0.0.1:{port}/",
                    password=SecretStr((root / "credential").read_text(encoding="ascii")),
                )
            )
            if pinned_inputs is not None:
                _require_native_inputs(scribe, root, pinned_inputs)
            error = _stop_native(manager, scribe, access, monkeypatch, receipt, expected_generation=pinned_generation)
            if pinned_inputs is not None:
                _require_native_inputs(scribe, root, pinned_inputs)
            assert not wait_for_jobs(
                systemctl=binaries["systemctl"],
                environment=environment,
                unique_stem="lychd-",
                timeout=_OBSERVATION_SECONDS,
            )
            after = read_properties(systemctl=binaries["systemctl"], environment=environment, units=units)
            receipt["after"] = after
            if retained is None:
                _assert_outcome(
                    manager=manager,
                    root=root,
                    case=case,
                    dependencies=dependencies,
                    after=after,
                    error=error,
                    receipt=receipt,
                )
            else:
                _assert_retained_stop(manager, error)
                _record_native_boot(root, receipt, native, before[_VESSEL]["InvocationID"])
                _write_json(root / "accounting-first-stop.json", retained.accounting())
                first_native = native
                assert not _same_process(first_native)
                assert not _group(first_native.pgid)
                assert _cgroup_members(control_group) == ()
                receipt["first_stop"] = {
                    key: receipt[key] for key in ("controller_calls", "admissions", "reference_events", "after")
                }
                assert pinned_inputs is not None
                _require_native_inputs(scribe, root, pinned_inputs)
                restarted = run_systemctl(
                    binaries["systemctl"], environment, "start", "--job-mode=fail", _VESSEL, timeout=5.0
                )
                assert restarted.returncode == 0, restarted.stderr
                _ready_retained(manager, retained)
                restarted_units = read_properties(systemctl=binaries["systemctl"], environment=environment, units=units)
                assert restarted_units[_VESSEL]["InvocationID"] != before[_VESSEL]["InvocationID"]
                native = _capture_native(restarted_units[_VESSEL], owned, outer["ControlGroup"])
                assert native.starttime != first_native.starttime or native.pid != first_native.pid
                control_group = restarted_units[_VESSEL]["ControlGroup"]
                _write_json(root / "accounting-restart.json", retained.accounting())
                second: dict[str, Any] = {}
                _require_native_inputs(scribe, root, pinned_inputs)
                error = _stop_native(
                    manager, scribe, access, monkeypatch, second, expected_generation=pinned_generation
                )
                _require_native_inputs(scribe, root, pinned_inputs)
                _assert_retained_stop(manager, error)
                _record_native_boot(root, receipt, native, restarted_units[_VESSEL]["InvocationID"])
                assert receipt["admissions"] == second["admissions"] == [pinned_generation]
                _write_json(root / "accounting-final-stop.json", retained.accounting())
                receipt["second_stop"] = second
            time.sleep(0.3)
            stable = read_properties(systemctl=binaries["systemctl"], environment=environment, units=units)
            receipt["after_restart_observation"] = stable
            assert stable[_VESSEL]["NRestarts"] == before[_VESSEL]["NRestarts"]
            assert stable[_VESSEL]["MainPID"] == "0"
            assert _listener_absent(port)
            assert not _same_process(native)
            assert not _group(native.pgid), "native process group survived normal retirement"
            assert _cgroup_members(control_group) == (), "native cgroup survived normal retirement"
            assert set(_outer_descendants(manager)) == {manager.process.pid, *bus_snapshot["process_pids"]}
            receipt["proof_success"] = True
        except BaseException as exc:
            receipt["primary_failure"] = {"type": type(exc).__name__}
            raise
        finally:
            try:
                if manager is not None:
                    receipt["controller_calls"] = manager.calls
                    _cleanup(manager, port, owned, native, control_group, receipt)
                elif child is not None:
                    stop_private_manager(child, Path(environment["XDG_RUNTIME_DIR"]))
                    receipt["cleanup"] = {"success": False, "reason": "manager occurrence identity was not captured"}
            finally:
                # Keep diagnostics, generated bytes and receipts for review;
                # destroy only the synthetic credential after process cleanup.
                try:
                    if receipt.get("cleanup", {}).get("success") is True:
                        (root / "credential").unlink(missing_ok=True)
                        shutil.rmtree(Path(environment["XDG_RUNTIME_DIR"]).parent.parent)
                finally:
                    try:
                        if scavenger_profile is not None:
                            _retain_native_log(root, receipt)
                    finally:
                        _write_json(root / "receipt.json", receipt)


def test_native_failed_clean_and_lost_reference_stop_under_private_systemd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require three ordered cases without altering installed units."""
    if os.environ.get("LYCHD_REQUIRE_SYSTEMD_PROOF") != "1":
        pytest.skip("native systemd stop proof requires explicit LYCHD_REQUIRE_SYSTEMD_PROOF=1")
    if sys.platform != "linux":
        proof_unavailable("native systemd stop proof requires Linux")
    assert not tmp_path.resolve().is_relative_to(_REPOSITORY), "explicit basetemp must be outside the checkout"
    assert tmp_path.resolve().is_relative_to(Path("/tmp")), "native fixture data must use ordinary private /tmp"  # noqa: S108 - explicitly admitted proof workspace
    binaries = required_binaries()
    _case(tmp_path / "01-close-failure", "close-failure", binaries, monkeypatch)
    _case(tmp_path / "02-clean", "clean", binaries, monkeypatch)
    _case(tmp_path / "03-reference-loss", "reference-loss", binaries, monkeypatch)


def test_retained_scavenger_native_vessel_stop_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qualify idle production publication and native Vessel lifetime only.

    Root independently admits exclusive external PostgreSQL/profile custody and
    the no-acquisition/no-model effect boundary. PostgreSQL stays live/preserved;
    existing inert cases separately own dependent-unit ordering/failure evidence.
    """
    if os.environ.get("LYCHD_REQUIRE_NATIVE_SCAVENGER_PROOF") != "1":
        pytest.skip("retained native Scavenger requires explicit proof opt-in")
    assert os.environ.get("LYCHD_REQUIRE_SYSTEMD_PROOF") == "1"
    assert sys.platform == "linux"
    assert not tmp_path.resolve().is_relative_to(_REPOSITORY)
    assert tmp_path.resolve().is_relative_to(Path("/tmp"))  # noqa: S108 - admitted private proof workspace
    profile = Path(os.environ["LYCHD_NATIVE_SCAVENGER_PROFILE"])
    assert profile.is_absolute()
    _case(tmp_path / "retained-scavenger", "clean", required_binaries(), monkeypatch, scavenger_profile=profile)
