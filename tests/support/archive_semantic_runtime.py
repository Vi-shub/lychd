"""Fixed opt-in Archive/model/PG composition; no deployed application claim.

The external operator owns admission and an independent terminal resource audit.
This helper never selects a warm service or downloads an image/model. Its two
children are the fixed loopback relay and the existing embedding lab's commands.
"""

# ruff: noqa: INP001 - existing tests/support namespace
# pyright: reportPrivateUsage=false
# Qualification reuses same-owned private native/PG test fixtures; production stays private.

from __future__ import annotations

import asyncio
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
import traceback
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from tests import scout_postgres_fixture as pg
from tests.support import embedding_runtime as native

PG_IMAGE = "sha256:1963bc48febf543433baa1ce3edcc6cc08154de722e22495f86681cc9a849026"
QUERY = "When does the observatory open on Saturday?"
SUPPORT = "The observatory opens at noon on Saturday."
CONTROL = "The river ferry leaves every thirty minutes."
CORRECTION = "The observatory opens at one in the afternoon on Saturday."
OPERATIONS = 8
POSTS = 16
NATIVE_TOKENS = 4088  # Eight fixed operations, each Core-bounded to at most511.
_SCRATCH_ROOT = Path("/tmp")  # noqa: S108 - admission boundary; fresh leaf and private owned ancestors are checked


def _sources() -> dict[str, str]:
    import hashlib

    names = (
        "pyproject.toml",
        "uv.lock",
        "tests/support/embedding_runtime.py",
        "tests/scout_postgres_fixture.py",
        "src/lychd/domain/archive/contracts.py",
        "src/lychd/domain/archive/derivation.py",
        "src/lychd/domain/archive/derivation_service.py",
        "src/lychd/domain/archive/semantic.py",
        "src/lychd/domain/archive/semantic_service.py",
        "src/lychd/db/archive.py",
        "src/lychd/db/archive_derivation.py",
        "src/lychd/db/archive_semantic.py",
        "src/lychd/db/models/archive.py",
        "src/lychd/db/models/archive_derivation.py",
        "src/lychd/interface/web/archive.py",
        "src/lychd/interface/web/archive_access.py",
        "src/lychd/interface/web/archive_derivation.py",
        "src/lychd/interface/web/archive_derivation_factory.py",
        "src/lychd/interface/web/archive_generation_factory.py",
        "src/lychd/interface/web/archive_semantic.py",
        "src/lychd/interface/web/archive_semantic_factory.py",
        "tests/support/archive_semantic_runtime.py",
        "tests/integration/test_archive_semantic_native_runtime.py",
        "tests/conftest.py",
        "src/lychd/config/settings/__init__.py",
        "src/lychd/config/settings/archive.py",
        "src/lychd/config/settings/root.py",
        "src/lychd/config/settings/server.py",
        "src/lychd/domain/codex/middleware.py",
        "src/lychd/domain/codex/local_access.py",
        "src/lychd/domain/codex/sigil.py",
        "src/lychd/domain/codex/guards.py",
        "src/lychd/interface/web/archive_response.py",
        "src/lychd/interface/web/openapi.py",
    )
    return {
        **native._source_hashes(),
        **{name: hashlib.sha256((native._ROOT / name).read_bytes()).hexdigest() for name in names},
    }


def persist(root: Path, name: str, value: Any) -> None:
    (root / name).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def _alarm(_number: int, _frame: Any) -> None:
    message = "archive_native_outer_deadline"
    raise TimeoutError(message)


def _admit_root(root: Path) -> Path:
    """Reject aliasing, protected/checkout roots and overlong PostgreSQL IPC first."""
    assert root.is_absolute(), "absolute_runtime_root"
    assert root.resolve() == root, "unaliased_runtime_root"
    assert root.is_relative_to(_SCRATCH_ROOT), "external_tmp_runtime_root"
    assert not root.is_relative_to(native._ROOT), "external_runtime_root"
    assert not root.exists(), "fresh_runtime_root"
    assert not root.is_symlink(), "fresh_runtime_root"
    for parent in root.parents:
        if parent == _SCRATCH_ROOT:
            break
        metadata = parent.stat()
        assert parent.is_dir(), "runtime_root_ancestor_directory"
        assert metadata.st_uid == os.getuid(), "runtime_root_ancestor_owner"
        assert metadata.st_mode & 0o022 == 0, "private_runtime_root_ancestor"
    socket = root / "pg-socket"
    assert len(os.fsencode(socket / ".s.PGSQL.5432")) <= 107, "PG_AF_UNIX_path_length"
    return socket


def _process(root: Path, receipt: dict[str, Any], actor: str, pid: int) -> None:
    """Serialize stable physical identity immediately, including partial starts."""
    before = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    value = {
        "pid": pid,
        "ppid": int(before[1]),
        "pgrp": int(before[2]),
        "session": int(before[3]),
        "start_ticks": int(before[19]),
        "cgroup": Path(f"/proc/{pid}/cgroup").read_text().strip(),
    }
    receipt.setdefault("processes", {})[actor] = value
    persist(root, "process-identities.json", receipt["processes"])
    after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    assert before[19] == after[19], "process_identity_changed"


def _model_processes(root: Path, lab: native._Lab, receipt: dict[str, Any]) -> None:
    pid, conmon_file = lab.inspect(".State.Pid", ".ConmonPidFile")
    receipt["model_process_observation"] = {"pid": pid, "conmon_pid_file": conmon_file}
    persist(root, "model-process-observation.json", receipt["model_process_observation"])
    if type(pid) is int and pid > 0:
        _process(root, receipt, "model", pid)
    if conmon_file:
        path = Path(conmon_file)
        assert lab.cid in path.parts
        assert path.name == "conmon.pid"
        _process(root, receipt, "conmon", int(path.read_text().strip()))


def _listener(process: subprocess.Popen[bytes]) -> int | None:
    inodes: set[str] = set()
    for item in Path(f"/proc/{process.pid}/fd").iterdir():
        try:
            target = item.readlink().as_posix()
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    rows = [line.split() for line in Path("/proc/net/tcp").read_text().splitlines()[1:]]
    owned = [row for row in rows if row[3] == "0A" and row[9] in inodes]
    if not owned:
        return None
    assert len(owned) == 1, "relay_listener"
    assert owned[0][1].startswith("0100007F:"), "relay_listener"
    return int(owned[0][1].split(":")[1], 16)


def _outer(lab: native._Lab) -> dict[str, Any]:
    """Refuse execution outside the operator's fixed, bounded process-tree unit."""
    unit = os.environ.get("LYCHD_ARCHIVE_NATIVE_OUTER_UNIT", "")
    assert re.fullmatch(r"lychd-archive-native-[0-9a-f]{32}\.service", unit), "outer_unit_required"
    raw = lab.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            unit,
            "--property=Id,InvocationID,MainPID,ControlGroup,RuntimeMaxUSec,TimeoutStopUSec,KillMode,Restart,TasksMax",
        ],
        timeout=3,
    )
    fields = dict(line.split("=", 1) for line in raw.splitlines())
    assert fields["Id"] == unit
    assert fields["InvocationID"]
    assert int(fields["MainPID"]) == os.getpid(), "outer_main_pid"
    assert fields["RuntimeMaxUSec"] == "5min"
    assert fields["TimeoutStopUSec"] == "30s"
    assert fields["KillMode"] == "control-group"
    assert fields["Restart"] == "no"
    assert fields["TasksMax"] == "512"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::" + fields["ControlGroup"]
    group = Path("/sys/fs/cgroup") / fields["ControlGroup"].lstrip("/")
    assert (group / "pids.max").read_text().strip() == "512"
    return fields


def _pg_custody(root: Path, container: Any, socket: Path, receipt: dict[str, Any]) -> None:
    """Observe exact bind and anonymous image-volume identities before client SQL."""
    container.reload()
    host = container.attrs["HostConfig"]
    receipt["mounts"] = container.attrs["Mounts"]
    receipt["state"] = {
        key: container.attrs["State"][key]
        for key in ("Pid", "Running", "Status", "StartedAt", "FinishedAt", "ExitCode", "Error")
    }
    receipt["privilege"] = {key: host[key] for key in ("Privileged", "Devices", "DeviceRequests")}
    receipt["volume_ids"] = [mount["Name"] for mount in receipt["mounts"] if mount["Type"] == "volume"]
    persist(root, "postgres-custody-before-assert.json", receipt)
    assert host["Privileged"] is False
    assert not host["Devices"]
    assert not host["DeviceRequests"]
    binds = [mount for mount in receipt["mounts"] if mount["Type"] == "bind"]
    assert len(binds) == 1
    assert binds[0]["Source"] == str(socket)
    assert binds[0]["Destination"] == "/scout-socket"
    assert binds[0]["RW"] is True
    assert binds[0]["Propagation"] == "rprivate"
    volumes = [mount for mount in receipt["mounts"] if mount["Type"] == "volume"]
    assert len(volumes) <= 4
    for mount in volumes:
        assert re.fullmatch(r"[0-9a-f]{64}", mount["Name"]), "anonymous_image_volume"
        assert mount["Destination"] in receipt["image_volume_destinations"]
        assert mount["RW"] is True
    assert len(binds) + len(volumes) == len(receipt["mounts"]), "unexpected_mount_type"


def _pg_limits(container: Any, receipt: dict[str, Any]) -> int:
    container.reload()
    host = container.attrs["HostConfig"]
    receipt["inspection"] = {
        name: host[name] for name in ("NetworkMode", "PortBindings", "Memory", "MemorySwap", "NanoCpus", "PidsLimit")
    }
    receipt["image_id"] = container.attrs["Image"]
    assert host["NetworkMode"] == "none"
    assert not host["PortBindings"]
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024**2
    assert host["NanoCpus"] == 1_000_000_000
    assert host["PidsLimit"] == 128
    assert container.attrs["Image"] == PG_IMAGE
    pid = container.attrs["State"]["Pid"]
    assert type(pid) is int
    assert pid > 0
    relative = Path(f"/proc/{pid}/cgroup").read_text().strip()
    assert relative.startswith("0::/")
    group = Path("/sys/fs/cgroup") / relative.removeprefix("0::/")
    values = {
        name: (group / name).read_text().strip() for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max")
    }
    receipt["kernel_limits"] = values
    quota, period = map(int, values["cpu.max"].split())
    assert values["memory.max"] == str(512 * 1024**2)
    assert values["memory.swap.max"] == "0"
    assert quota == period
    assert values["pids.max"] == "128"
    return pid


def _start_model(
    root: Path, lab: native._Lab, fixture: Path, manifest: dict[str, Any], receipt: dict[str, Any]
) -> None:
    lab.cid = lab.podman(*native.create_arguments(lab.name, lab.token, fixture, manifest))
    receipt["model_container_id"] = lab.cid
    receipt["model_ownership_token"] = lab.token
    assert lab.identity() == [lab.cid, lab.token, False]
    lab.start_attempted = True
    try:
        lab.podman("start", lab.cid)
    finally:
        _model_processes(root, lab, receipt)
    lab.capture_handles()
    for _ in range(40):
        assert time.monotonic() < lab.deadline
        assert lab.identity()[2] is True
        try:
            health = lab.inside(
                "/usr/bin/curl",
                "--disable",
                "--silent",
                "--show-error",
                "--fail",
                "--noproxy",
                "*",
                "--proxy",
                "",
                "--connect-timeout",
                "1",
                "--max-time",
                "1",
                "--max-filesize",
                "4096",
                "--proto",
                "=http",
                "http://127.0.0.1:8080/health",
            )
            if json.loads(health).get("status") == "ok":
                return
        except (AssertionError, ValueError):
            pass
        time.sleep(0.25)
    message = "model_readiness"
    raise AssertionError(message)


def run_native_semantic_proof(  # noqa: C901, PLR0912, PLR0915 - one outer owner must retain every partial resource through independent cleanup
    root: Path, monkeypatch: pytest.MonkeyPatch, workflow: Any
) -> dict[str, Any]:
    """Own only one fresh model, one PG and one namespace-FD-pinned relay."""
    if os.environ.get("LYCHD_REQUIRE_ARCHIVE_SEMANTIC_NATIVE_PROOF") != "1":
        pytest.skip("set LYCHD_REQUIRE_ARCHIVE_SEMANTIC_NATIVE_PROOF=1 for the fixed public proof")
    value = os.environ.get("LYCHD_EMBEDDING_FIXTURE")
    assert value, "existing public LYCHD_EMBEDDING_FIXTURE required"
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "exclusive_alarm_required"
    assert Path(__file__).resolve() == native._ROOT / "tests/support/archive_semantic_runtime.py"
    assert Path(workflow.__code__.co_filename).resolve() == (
        native._ROOT / "tests/integration/test_archive_semantic_native_runtime.py"
    )
    socket = _admit_root(root)
    root.mkdir(mode=0o700)
    manifest = native.qualification()
    fixture = Path(value)
    identity = native.verify_artifact(fixture, size=manifest["model_bytes"], digest=manifest["model_sha256"])
    sources = _sources()
    native._verify_imports()
    lab = native._Lab(root, native.required_tools())
    receipt: dict[str, Any] = {
        "status": "failed",
        "cleanup": "pending",
        "source_sha256": sources,
        "limits": {
            "operations": OPERATIONS,
            "native_input_tokens": NATIVE_TOKENS,
            "embedding_POSTs": POSTS,
            "outer_seconds": 300,
            "model_seconds": 180,
            "cleanup_seconds": 30,
        },
        "model_sha256": manifest["model_sha256"],
        "image_id": manifest["image_id"],
        "evidence_boundary": (
            "Authenticated in-process ASGI + actual registered CPU model + actual PG; "
            "no deployed Vessel/CLI/browser/Bridge or general quality claim."
        ),
        "postgres": {},
        "relay": {},
        "independent_terminal_audit": "required_from_operator",
    }
    relay = None
    relay_fd = None
    relay_port = None
    namespace_fds: list[int] = []
    client = None
    pg_fd = None
    pg_name = "lychd-archive-native-" + lab.token
    pg_id = None
    create_attempted = False
    receipt["model_container_name"] = lab.name
    receipt["model_ownership_token"] = lab.token
    receipt["postgres"]["name"] = pg_name
    persist(root, "resources-admission.json", receipt)
    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, 300)
    try:
        receipt["outer_unit"] = _outer(lab)
        info = lab.podman(
            "info",
            "--format",
            "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupManager}} {{.Store.GraphDriverName}}",
        )
        assert info == "true v2 systemd overlay"
        store = lab.podman("info", "--format", "{{json .Store.GraphRoot}}\n{{json .Store.RunRoot}}")
        receipt["ordinary_store"] = [json.loads(line) for line in store.splitlines()]
        assert receipt["ordinary_store"] == [
            str(Path(lab.environment["XDG_DATA_HOME"]) / "containers/storage"),
            f"/run/user/{os.getuid()}/containers",
        ]
        image = lab.podman("image", "inspect", "--format", "{{json .ID}}\n{{json .RepoDigests}}", manifest["image_id"])
        native.require_image([json.loads(line) for line in image.splitlines()], manifest)
        client = pg._client()
        pg_image = client.images.get(PG_IMAGE)
        assert pg_image.id == PG_IMAGE
        receipt["postgres"]["image_volume_destinations"] = sorted(pg_image.attrs["Config"]["Volumes"] or {})
        socket.mkdir(mode=0o777)
        socket.chmod(0o777)
        database = client.containers.create(
            PG_IMAGE,
            name=pg_name,
            labels={pg._LABEL: pg_name},
            command=[
                "postgres",
                "-c",
                "unix_socket_directories=/var/run/postgresql,/scout-socket",
                "-c",
                "listen_addresses=",
            ],
            environment={"POSTGRES_USER": "scout", "POSTGRES_PASSWORD": "fixture", "POSTGRES_DB": "scout"},
            network_mode="none",
            use_config_proxy=False,
            volumes={str(socket): {"bind": "/scout-socket", "mode": "rw,Z"}},
            mem_limit="512m",
            memswap_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=128,
        )
        pg_id = database.id
        receipt["postgres"]["container_id"] = pg_id
        try:
            database.start()
        finally:
            database.reload()
            partial_pid = database.attrs["State"]["Pid"]
            if type(partial_pid) is int and partial_pid > 0:
                _process(root, receipt, "postgres", partial_pid)
            _pg_custody(root, database, socket, receipt["postgres"])
        pg_pid = database.attrs["State"]["Pid"]
        assert type(pg_pid) is int
        assert pg_pid > 0
        pg_fd = os.pidfd_open(pg_pid)
        receipt["postgres"]["pid"] = pg_pid
        assert _pg_limits(database, receipt["postgres"]) == pg_pid
        pg._ready(client, database)
        create_attempted = True
        _start_model(root, lab, fixture, manifest, receipt)
        receipt["runtime"] = native._attest_runtime(lab, fixture, manifest)
        status = dict(
            line.split(":", 1) for line in Path(f"/proc/{lab.pid}/status").read_text().splitlines() if ":" in line
        )
        receipt["runtime"]["process_status"] = {
            key: status[key].strip() for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")
        }
        assert all(int(status[key].strip(), 16) == 0 for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
        assert status["NoNewPrivs"].strip() == "1"
        for name in ("user", "net"):
            fd = os.open(f"/proc/{lab.pid}/ns/{name}", os.O_RDONLY | os.O_CLOEXEC)
            namespace_fds.append(fd)
            assert os.fstat(fd).st_ino == Path(f"/proc/{lab.pid}/ns/{name}").stat().st_ino
        assert lab.identity() == [lab.cid, lab.token, True]
        receipt["relay"]["namespace_inodes"] = [os.fstat(fd).st_ino for fd in namespace_fds]
        inner = [
            "/usr/bin/nsenter",
            f"--user=/proc/{os.getpid()}/fd/{namespace_fds[0]}",
            f"--net=/proc/{os.getpid()}/fd/{namespace_fds[1]}",
            "/usr/bin/socat",
            "-T3",
            "STDIO",
            "TCP4:127.0.0.1:8080",
        ]
        argv = [
            "/usr/bin/prlimit",
            "--core=0:0",
            "--cpu=5:5",
            "--as=268435456:268435456",
            "--fsize=65536:65536",
            "--nofile=64:64",
            "--",
            "/usr/bin/socat",
            "-T3",
            "TCP4-LISTEN:0,bind=127.0.0.1,fork,max-children=2",
            "EXEC:'" + " ".join(inner) + "',nofork",
        ]
        with (root / "relay.stdout").open("xb") as stdout, (root / "relay.stderr").open("xb") as stderr:
            relay = subprocess.Popen(  # noqa: S603 - fixed reviewed argv
                argv,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
        receipt["relay"].update(pid=relay.pid, argv=argv)
        _process(root, receipt, "relay", relay.pid)
        relay_fd = os.pidfd_open(relay.pid)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            assert not select.select([relay_fd], [], [], 0)[0], "relay_exited"
            relay_port = _listener(relay)
            if relay_port is not None:
                break
            time.sleep(0.01)
        assert relay_port is not None, "relay_not_ready"
        receipt["relay"]["port"] = relay_port
        receipt["relay"]["observed_process_limits"] = Path(f"/proc/{relay.pid}/limits").read_text()
        assert set(receipt["processes"]) == {"model", "conmon", "postgres", "relay"}, "physical_identity_set"
        persist(root, "resources-before-work.json", receipt)

        async def bounded() -> Any:
            async with asyncio.timeout(min(105, max(0.01, lab.deadline - time.monotonic() - 5))):
                return await workflow(root, socket, relay_port, monkeypatch, receipt)

        receipt["proof"] = asyncio.run(bounded())
        assert lab.identity() == [lab.cid, lab.token, True]
        assert all(not select.select([fd], [], [], 0)[0] for fd in lab.handles)
        assert _pg_limits(database, receipt["postgres"]) == pg_pid
        assert _sources() == sources
        native._verify_imports()
        receipt["status"] = "passed"
    except BaseException:
        receipt["failure"] = traceback.format_exc()
        if (root / "last-command.json").exists():
            (root / "failed-command.json").write_bytes((root / "last-command.json").read_bytes())
        persist(root, "failure-before-cleanup.json", receipt)
        raise
    finally:
        cleanup_end = time.monotonic() + 30
        signal.setitimer(signal.ITIMER_REAL, 30)
        errors: list[str] = []

        def cleanup_relay() -> None:
            if relay is not None:
                with suppress(ProcessLookupError):
                    # The unreaped leader reserves this exclusively owned group ID.
                    os.killpg(relay.pid, signal.SIGKILL)
                relay.wait(timeout=3)
                assert relay_fd is not None
                assert select.select([relay_fd], [], [], 0)[0]
                if relay_port is not None:
                    rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
                    assert not any(
                        row.split()[1] == f"0100007F:{relay_port:04X}" and row.split()[3] == "0A" for row in rows
                    )
                receipt["relay"]["leader_settled"] = True

        def cleanup_model() -> None:
            if create_attempted:
                lab.cleanup()
                receipt["model_container_and_processes_settled"] = True

        def cleanup_database() -> None:
            if client is not None:
                pg._remove(client, pg_name, pg_id, receipt["postgres"])
                if pg_fd is not None:
                    assert select.select([pg_fd], [], [], 1)[0], "PG_process_alive"
            if socket.exists():
                shutil.rmtree(socket)
            assert not socket.exists()

        try:
            for cleanup in (cleanup_relay, cleanup_model, cleanup_database):
                try:
                    assert time.monotonic() < cleanup_end, "cleanup_deadline"
                    cleanup()
                except BaseException:  # noqa: BLE001 - every independent cleanup still runs; failure remains fatal
                    errors.append(traceback.format_exc())
            if not errors and "outer_unit" in receipt:
                group = Path("/sys/fs/cgroup") / receipt["outer_unit"]["ControlGroup"].lstrip("/")
                remaining = sorted(map(int, (group / "cgroup.procs").read_text().split()))
                receipt["outer_processes_after_cleanup"] = remaining
                if remaining != [os.getpid()]:
                    errors.append("Outer unit still contains descendants; independent reconciliation required.")
            receipt["cleanup"] = "failed" if errors else "passed"
            if errors:
                receipt["status"] = "failed"
                message = "archive_native_cleanup_failed; independent reconciliation required"
                raise AssertionError(message)
        finally:
            if client is not None:
                client.close()
            receipt["cleanup_errors"] = errors
            descriptors = [*namespace_fds, *lab.handles]
            if relay_fd is not None:
                descriptors.append(relay_fd)
            if pg_fd is not None:
                descriptors.append(pg_fd)
            for fd in descriptors:
                os.close(fd)
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            try:
                assert (
                    native.verify_artifact(fixture, size=manifest["model_bytes"], digest=manifest["model_sha256"])
                    == identity
                )
                receipt["artifact_unchanged_after_cleanup"] = True
            except BaseException:
                receipt["status"] = "failed"
                receipt["artifact_unchanged_after_cleanup"] = False
                raise
            finally:
                receipt["elapsed_seconds"] = round(time.monotonic() - lab.started, 3)
                persist(root, "receipt.json", receipt)
    return receipt
