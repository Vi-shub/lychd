"""One fixed opt-in public embedding lab; also its isolated child entry point.

Uses the ordinary rootless image cache, never production units/configuration.
RLIMITs are process limits, not cgroup quotas. Fixture hashing is outside the
175-second work clock; cleanup has a separate30-second budget. The supplied
public file must remain quiescent. No artifact metadata or labels are changed.
"""

# ruff: noqa: INP001 - existing tests/support namespace

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import select
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import redirect_stdout, suppress
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_ROOT = Path(__file__).resolve().parents[2]
_LABEL = "org.lychd.embedding-proof"
_OUTPUT_LIMIT = 65536
_SOURCE_FILES = (
    "domain/animation/embeddings.py",
    "domain/animation/services/registry.py",
    "domain/cortex/dispatcher.py",
    "domain/cortex/leases.py",
    "extensions/builtin/animator/llamacpp/embedding.py",
    "extensions/builtin/animator/llamacpp/embedding_profile.py",
    "extensions/builtin/animator/llamacpp/connector.py",
    "extensions/builtin/animator/runtimes/llamacpp.py",
    "extensions/builtin/animator/soulstones/llamacpp.py",
)


def qualification() -> dict[str, Any]:
    """Read the tracked immutable manual qualification, never a user manifest."""
    from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE

    raw = (_ROOT / "tests/fixtures/embeddings/qwen3-06b-q8-b9445.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PROFILE.conformance_digest, "qualification_digest"
    manifest = cast("dict[str, Any]", json.loads(raw))
    assert manifest["model_sha256"] == PROFILE.model_sha256
    assert manifest["image_manifest"] == PROFILE.image_digest
    assert manifest["engine_revision"] == PROFILE.engine_revision
    return manifest


def verify_artifact(path: Path, *, size: int, digest: str) -> tuple[int, int, int, int]:
    """Hash a regular non-symlink file without writing bytes, modes or xattrs."""
    assert path.is_absolute(), "absolute unaliased artifact required"
    assert path.resolve() == path, "absolute unaliased artifact required"
    assert not any(char in str(path) for char in (",", "\n", "\r", "\0")), "artifact_mount_path"
    before = path.lstat()
    assert stat.S_ISREG(before.st_mode), "artifact_size_or_type"
    assert before.st_size == size, "artifact_size_or_type"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        assert (opened.st_dev, opened.st_ino, opened.st_size) == (before.st_dev, before.st_ino, size)
        assert hashlib.file_digest(stream, "sha256").hexdigest() == digest, "artifact_digest"
        after = os.fstat(stream.fileno())
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == identity, "artifact_changed"
    current = path.lstat()
    assert (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) == identity, "artifact_replaced"
    return identity


def required_tools() -> dict[str, str]:
    from lychd.system.host_tools import trusted_host_tool

    assert sys.platform == "linux", "rootless Linux required"
    assert os.getuid() != 0, "rootless Linux required"
    assert hasattr(os, "pidfd_open"), "rootless Linux required"
    resolved: dict[str, str] = {}
    for name in ("podman", "nsenter", "env"):
        path = trusted_host_tool(name, fallbacks=(Path("/usr/bin") / name, Path("/bin") / name))
        assert path is not None, f"trusted {name} required"
        resolved[name] = path
    return resolved


def _stone() -> Any:
    from lychd.extensions.builtin.animator.llamacpp.embedding_profile import IMAGE, PROFILE
    from lychd.extensions.builtin.animator.soulstones.llamacpp import LlamaCppSoulstoneConfig

    return LlamaCppSoulstoneConfig.model_validate(
        {
            "name": "embedding-proof",
            "port": 8080,
            "base_url": "http://127.0.0.1:8080/v1",
            "embedding_profile": PROFILE.profile_id,
            "model_path": "/models/embedding.gguf",
            "quadlet": {"image": IMAGE},
        }
    )


def create_arguments(name: str, token: str, fixture: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Plan the fixed runtime with no host listeners, GPU or fixture relabel."""
    from lychd.extensions.builtin.animator.llamacpp.embedding_profile import profile_command

    return [
        "create",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"{_LABEL}={token}",
        "--timeout=180",
        "--network=none",
        "--ipc=private",
        "--pid=private",
        "--read-only",
        "--read-only-tmpfs=false",
        "--pids-limit=64",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--ulimit=cpu=60:60",
        "--no-healthcheck",
        "--log-driver=k8s-file",
        "--log-opt=max-size=64k",
        "--image-volume=ignore",
        "--unsetenv-all",
        "--http-proxy=false",
        "--env=PATH=/usr/bin:/bin",
        "--env=LANG=C",
        "--entrypoint=/bin/sh",
        f"--mount=type=bind,source={fixture},destination=/models/embedding.gguf,ro",
        str(manifest["image_id"]),
        "-ec",
        'ulimit -v 4194304; exec "$@"',
        "lychd-limited",
        "/app/llama-server",
        *profile_command(_stone()),
    ]


def require_image(values: list[Any], manifest: Mapping[str, Any]) -> None:
    assert len(values) == 2, "image_inspection"
    assert str(values[0]).removeprefix("sha256:") == str(manifest["image_id"]).removeprefix("sha256:"), "image_identity"
    assert isinstance(values[1], list), "image_manifest"
    assert manifest["image_manifest"] in values[1], "image_manifest"


def _environment(root: Path) -> dict[str, str]:
    import pwd

    host = pwd.getpwuid(os.getuid())
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path(host.pw_dir) / ".local/share")))
    assert data.is_absolute(), "absolute XDG_DATA_HOME required"
    graphroot = data / "containers/storage"
    runroot = Path(f"/run/user/{os.getuid()}/containers")
    config = root / "config/containers"
    for directory in (root / "home/.config/containers", config):
        directory.mkdir(parents=True)
        (directory / "mounts.conf").write_text("")
        (directory / "registries.conf").write_text("unqualified-search-registries=[]\n")
        (directory / "auth.json").write_text('{"auths":{}}\n')
    # CONTAINERS_CONF replaces system/user defaults; no hooks, device/env/volume
    # defaults or override variable can expand this fixed test's create argv.
    (config / "containers.conf").write_text(
        "[containers]\ndevices=[]\nvolumes=[]\nenv=[]\nprivileged=false\n"
        '[engine]\ncgroup_manager="systemd"\nevents_logger="file"\nhooks_dir=[]\n'
    )
    # Author only a task-local selector for the existing ordinary store. No
    # additional image store, private export/load, or original config rewrite.
    (config / "storage.conf").write_text(
        '[storage]\ndriver="overlay"\n'
        f"graphroot={json.dumps(str(graphroot))}\nrunroot={json.dumps(str(runroot))}\n"
        "[storage.options]\nadditionalimagestores=[]\n"
    )
    return {
        "CONTAINERS_CONF": str(config / "containers.conf"),
        "CONTAINERS_STORAGE_CONF": str(config / "storage.conf"),
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(data),
        "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
        "USER": host.pw_name,
        "LOGNAME": host.pw_name,
        "LANG": "C",
        "PATH": "/usr/bin:/bin",
    }


def _wait_successful_leader(process: subprocess.Popen[bytes], cutoff: float) -> None:
    # Observe without reaping: a failed leader still reserves the process-group
    # identity while the caller kills any surviving children.
    observed = None
    while observed is None:
        assert time.monotonic() < cutoff, "command_deadline"
        observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if observed is None:
            time.sleep(0.01)
    assert observed.si_code == os.CLD_EXITED, "command_failed"
    assert observed.si_status == 0, "command_failed"
    process.wait(timeout=max(0.01, cutoff - time.monotonic()))


class _Lab:
    """Bound one lab's fixed commands and preserve owned group identity on failure."""

    def __init__(self, root: Path, tools: dict[str, str]) -> None:
        self.root, self.tools = root, tools
        self.environment = _environment(root)
        self.started = time.monotonic()
        self.deadline = self.started + 175
        self.commands = 0
        self.cleaning = False
        self.cid = ""
        self.token = uuid4().hex
        self.name = "lychd-embedding-proof-" + self.token
        self.handles: list[int] = []
        self.pid = ""
        self.start_attempted = False

    def run(self, argv: Sequence[str], *, timeout: float = 25) -> str:
        cutoff = min(self.deadline, time.monotonic() + timeout)
        assert time.monotonic() < cutoff, "command_deadline"
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        process: subprocess.Popen[bytes] | None = None
        self.commands += 1
        assert self.commands <= (20 if self.cleaning else 160), "command_budget"
        with selectors.DefaultSelector() as reader:
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed reviewed argv; never shell=True
                    argv,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                )
                assert process.stdout
                assert process.stderr
                for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    reader.register(stream, selectors.EVENT_READ, name)
                while reader.get_map():
                    assert time.monotonic() < cutoff, "command_deadline"
                    for key, _ in reader.select(min(0.2, max(0, cutoff - time.monotonic()))):
                        part = os.read(key.fd, 8192)
                        if not part:
                            reader.unregister(key.fileobj)
                            continue
                        assert sum(map(len, captured.values())) + len(part) <= _OUTPUT_LIMIT, "command_output_bound"
                        captured[key.data].extend(part)
                _wait_successful_leader(process, cutoff)
                return captured["stdout"].decode()
            except BaseException:
                # Until wait succeeds the unreaped leader reserves this group ID.
                if process is not None and process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                raise
            finally:
                (self.root / "last-command.json").write_text(
                    json.dumps(
                        {
                            "argv": list(argv),
                            **{key: value.decode(errors="replace") for key, value in captured.items()},
                        },
                        indent=2,
                    )
                    + "\n"
                )
                if process is not None:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    def podman(self, *args: str, timeout: float = 25) -> str:
        return self.run([self.tools["podman"], "--remote=false", *args], timeout=timeout).strip()

    def inspect(self, *fields: str, selector: str | None = None) -> list[Any]:
        template = "\n".join("{{json " + field + "}}" for field in fields)
        return [
            json.loads(line)
            for line in self.podman(
                "inspect", "--type", "container", "--format", template, selector or self.cid
            ).splitlines()
        ]

    def identity(self, selector: str | None = None) -> list[Any]:
        values = self.inspect(".ID", f'(index .Config.Labels "{_LABEL}")', ".State.Running", selector=selector)
        assert len(values) == 3, "ownership"
        assert re.fullmatch("[0-9a-f]{64}", values[0]), "ownership"
        assert values[1] == self.token, "ownership"
        if self.cid:
            assert values[0] == self.cid, "container_identity_changed"
        return values

    def inside(self, *args: str) -> str:
        return self.podman("exec", self.cid, "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C", *args)

    def capture_handles(self) -> None:
        pid, conmon = self.inspect(".State.Pid", ".ConmonPidFile")
        self.pid = str(pid)
        path = Path(conmon)
        assert self.cid in path.parts, "conmon_identity"
        assert path.name == "conmon.pid", "conmon_identity"
        for value in (self.pid, path.read_text().strip()):
            assert value.isdigit(), "owned_pid"
            assert int(value) > 0, "owned_pid"
            self.handles.append(os.pidfd_open(int(value)))
        assert len(self.handles) == 2

    def cleanup(self) -> None:
        self.deadline = time.monotonic() + 30
        self.commands = 0
        self.cleaning = True
        tracking_complete = not self.start_attempted or len(self.handles) == 2
        # Cleanup does not initialize a different engine, remove images or signal
        # the ordinary cache engine's shared pause process.
        candidates = self.podman(
            "ps", "--all", "--filter", f"name=^{self.name}$", "--format", "{{.ID}}", "--no-trunc"
        ).splitlines()
        if candidates:
            assert len(candidates) == 1
            cid, _, running = self.identity(candidates[0])
            self.cid = cid
            with suppress(Exception):
                self.podman("logs", "--tail=100", self.cid)
                (self.root / "server-log.json").write_text((self.root / "last-command.json").read_text())
            if running:
                if len(self.handles) != 2:
                    for descriptor in self.handles:
                        os.close(descriptor)
                    self.handles.clear()
                    # Failure to obtain both identities must not prevent stop/rm;
                    # report incomplete process verification after removal.
                    with suppress(Exception):
                        self.capture_handles()
                    tracking_complete = len(self.handles) == 2
                self.podman("stop", "--time=2", self.cid)
            assert self.identity()[2] is False
            self.podman("rm", self.cid)
        assert not self.podman("ps", "--all", "--filter", f"name=^{self.name}$", "--format", "{{.ID}}")
        for descriptor in self.handles:
            assert select.select([descriptor], [], [], min(3, max(0, self.deadline - time.monotonic())))[0], (
                "owned_process_alive"
            )
        assert tracking_complete, "owned_process_tracking_incomplete"


def _attest_runtime(lab: _Lab, fixture: Path, manifest: Mapping[str, Any]) -> dict[str, object]:
    from lychd.extensions.builtin.animator.llamacpp.embedding_profile import profile_command

    assert lab.identity() == [lab.cid, lab.token, True]
    image, network, ports, pids, mounts, pid = lab.inspect(
        ".Image",
        ".HostConfig.NetworkMode",
        ".HostConfig.PortBindings",
        ".HostConfig.PidsLimit",
        ".Mounts",
        ".State.Pid",
    )
    assert str(image).removeprefix("sha256:") == str(manifest["image_id"]).removeprefix("sha256:")
    assert network == "none"
    assert not ports
    assert pids == 64
    assert str(pid) == lab.pid
    assert len(mounts) == 1
    assert mounts[0]["Source"] == str(fixture)
    assert mounts[0]["Destination"] == "/models/embedding.gguf"
    assert mounts[0]["RW"] is False
    raw = lab.inside("/bin/cat", "/proc/1/cmdline")
    assert raw.strip("\0").split("\0") == ["/app/llama-server", *profile_command(_stone())], "server_argv"
    limits = lab.inside("/bin/cat", "/proc/1/limits")
    assert re.search(r"^Max cpu time\s+60\s+60\s+seconds[ \t]*$", limits, re.MULTILINE), "cpu_rlimit"
    assert re.search(r"^Max address space\s+4294967296\s+4294967296\s+bytes[ \t]*$", limits, re.MULTILINE), "as_rlimit"
    # This engine prints its version on stderr, so use a fixed container shell
    # only to join those two bounded streams. No user text is interpolated.
    version = lab.inside("/bin/sh", "-c", "/app/llama-server --version 2>&1")
    match = re.search(r"version:\s*(?:b)?9445\s*\(([0-9a-f]{7,40})\)", version)
    assert match, "binary_revision"
    assert str(manifest["engine_revision"]).startswith(match.group(1)), "binary_revision"
    assert all(not select.select([fd], [], [], 0)[0] for fd in lab.handles)
    return {
        "server_argv_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "version": match.group(0),
        "cpu_seconds": 60,
        "address_space_bytes": 4294967296,
        "pids": pids,
        "cgroup_quota_claim": False,
    }


def run_registered_embedding_proof(root: Path) -> dict[str, Any]:  # noqa: PLR0915 - one explicit owned runtime lifetime
    """Run only after explicit opt-in; preserve failure and cleanup evidence."""
    if os.environ.get("LYCHD_REQUIRE_EMBEDDING_PROOF") != "1":
        pytest.skip("set LYCHD_REQUIRE_EMBEDDING_PROOF=1 for the public runtime proof")
    value = os.environ.get("LYCHD_EMBEDDING_FIXTURE")
    assert value, "LYCHD_EMBEDDING_FIXTURE must name the existing public artifact"
    manifest = qualification()
    fixture = Path(value)
    identity = verify_artifact(fixture, size=manifest["model_bytes"], digest=manifest["model_sha256"])
    tools = required_tools()
    root.mkdir(mode=0o700)
    lab = _Lab(root, tools)
    receipt: dict[str, Any] = {
        "status": "failed",
        "cleanup": "pending",
        "initial_fixture_hash_before_work_clock": True,
        "final_fixture_hash_after_cleanup": False,
        "container_wall_seconds": 180,
        "parent_work_seconds": 175,
        "cleanup_budget_seconds": 30,
        "model_sha256": manifest["model_sha256"],
        "image_id": manifest["image_id"],
        "image_manifest": manifest["image_manifest"],
        "evidence_boundary": "Externally started isolated runtime plus actual registry/Dispatcher/HTTPX; no Orchestrator activation or Archive proof.",
    }
    create_attempted = False
    try:
        info = lab.podman(
            "info",
            "--format",
            "{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}} {{.Host.CgroupManager}} {{.Store.GraphDriverName}}",
        )
        assert info == "true v2 systemd overlay", "supported_rootless_engine"
        store = lab.podman("info", "--format", "{{json .Store.GraphRoot}}\n{{json .Store.RunRoot}}")
        assert [json.loads(line) for line in store.splitlines()] == [
            str(Path(lab.environment["XDG_DATA_HOME"]) / "containers/storage"),
            f"/run/user/{os.getuid()}/containers",
        ], "ordinary_store_identity"
        image = lab.podman("image", "inspect", "--format", "{{json .ID}}\n{{json .RepoDigests}}", manifest["image_id"])
        require_image([json.loads(line) for line in image.splitlines()], manifest)
        create_attempted = True
        created = lab.podman(*create_arguments(lab.name, lab.token, fixture, manifest))
        assert re.fullmatch(r"[0-9a-f]{64}", created), "container_id"
        lab.cid = created
        assert lab.identity() == [lab.cid, lab.token, False]
        lab.start_attempted = True
        lab.podman("start", lab.cid)
        lab.capture_handles()
        ready = False
        while time.monotonic() < min(lab.started + 60, lab.deadline):
            assert lab.identity()[2] is True, "server_exited"
            try:
                raw = lab.inside(
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
                    "2",
                    "--max-time",
                    "2",
                    "--max-filesize",
                    "4096",
                    "--proto",
                    "=http",
                    "http://127.0.0.1:8080/health",
                )
                ready = json.loads(raw).get("status") == "ok"
            except (AssertionError, ValueError):
                pass
            if ready:
                break
            time.sleep(0.5)
        assert ready, "runtime_not_ready (check existing fixture SELinux context; it is never relabeled)"
        receipt["runtime"] = _attest_runtime(lab, fixture, manifest)
        namespace = lab.inside("/usr/bin/readlink", "/proc/1/ns/net")
        lab.deadline = min(lab.deadline, lab.started + 165)
        child = [
            tools["podman"],
            "--remote=false",
            "unshare",
            tools["nsenter"],
            f"--net=/proc/{lab.pid}/ns/net",
            tools["env"],
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C",
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            namespace,
        ]
        result = cast("dict[str, Any]", json.loads(lab.run(child, timeout=65)))
        assert result["status"] == "passed"
        assert result["network_namespace"] == namespace
        assert lab.identity() == [lab.cid, lab.token, True]
        assert all(not select.select([fd], [], [], 0)[0] for fd in lab.handles)
        receipt["proof"] = result
        receipt["status"] = "passed"
    except BaseException as exc:
        receipt["failure_type"] = type(exc).__name__
        diagnostic = root / "last-command.json"
        if diagnostic.exists():
            (root / "failed-command.json").write_text(diagnostic.read_text())
        raise
    finally:
        try:
            if create_attempted:
                lab.cleanup()
            receipt["cleanup"] = "passed"
        except BaseException as exc:
            receipt["cleanup"] = "failed:" + type(exc).__name__
            receipt["status"] = "failed"
            raise
        finally:
            _finish_receipt(lab, receipt, fixture, identity, manifest)
    return receipt


def _finish_receipt(
    lab: _Lab, receipt: dict[str, Any], fixture: Path, identity: tuple[int, int, int, int], manifest: Mapping[str, Any]
) -> None:
    """Close owned handles and retain final byte identity after runtime cleanup."""
    for descriptor in lab.handles:
        os.close(descriptor)
    receipt["tracked_processes"] = len(lab.handles)
    receipt["container_id"] = lab.cid
    receipt["ownership_label"] = lab.token
    try:
        if receipt["status"] == "passed":
            assert identity == verify_artifact(
                fixture, size=manifest["model_bytes"], digest=manifest["model_sha256"]
            ), "artifact_changed_after_use"
            receipt["final_fixture_hash_after_cleanup"] = True
    except BaseException as exc:
        receipt["status"] = "failed"
        receipt["failure_type"] = type(exc).__name__
        raise
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - lab.started, 3)
        (lab.root / "receipt.json").write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n")


def _source_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((_ROOT / "src/lychd" / name).read_bytes()).hexdigest() for name in _SOURCE_FILES}


async def _driver_probe() -> dict[str, Any]:  # noqa: PLR0915 - fixed positive and negative controls
    from lychd.domain.animation.embeddings import (
        EmbeddingBinding,
        EmbeddingInput,
        EmbeddingOperation,
        EmbeddingRefused,
        EmbeddingVector,
        embedding_vector_digest,
    )
    from lychd.domain.animation.services.declarations import AnimatorDeclarations
    from lychd.domain.animation.services.registry import AnimatorRegistry
    from lychd.domain.cortex.dispatcher import Dispatcher
    from lychd.domain.cortex.leases import LeaseLedger
    from lychd.extensions.builtin.animator.llamacpp.embedding_profile import PROFILE
    from lychd.extensions.builtin.animator.runtimes.llamacpp import LlamaCppRuntimeAdapter

    before = _source_hashes()
    _verify_imports()
    registry = AnimatorRegistry(
        declarations=AnimatorDeclarations(soulstones=(_stone(),), portals=()),
        runtime_adapters=(LlamaCppRuntimeAdapter(),),
    )
    registry.load()
    specs = registry.list_capabilities()
    assert len(specs) == 1
    binding = EmbeddingBinding(capability_key=specs[0].key, profile_digest=PROFILE.digest)
    leases = LeaseLedger()
    dispatcher = Dispatcher(registry, leases=leases)

    def item(text: str, key: str) -> EmbeddingInput:
        return EmbeddingInput(key, hashlib.sha256(text.encode()).hexdigest(), text)

    outputs: list[EmbeddingVector] = []
    checks: list[int] = []
    fixtures: tuple[tuple[EmbeddingOperation, str, str], ...] = (
        ("embed_document", "Paris is the capital of France.", "document:france"),
        ("embed_document", "A guitar has vibrating strings and produces music.", "document:music"),
        ("embed_query", "What is the capital of France?", "query:france"),
    )
    for operation, text, key in fixtures:
        local_checks: list[int] = []
        async with dispatcher.lease_embedding(binding, operation=operation, run_id="public-proof") as grant:
            assert len(leases.active()) == 1
            batch = await grant.driver.embed((item(text, key),), authorize=partial(local_checks.append, 1))
            assert batch.profile_digest == PROFILE.digest
            assert batch.operation == operation
            assert len(batch.items) == 1
            assert batch.items[0].item_id == key
            vector = batch.items[0]
            assert len(vector.vector) == 1024
            assert math.isclose(math.sqrt(math.fsum(v * v for v in vector.vector)), 1.0, abs_tol=0.0001)
            assert vector.vector_digest == embedding_vector_digest(vector.vector)
            outputs.append(vector)
        assert not leases.active()
        assert len(local_checks) == 3
        checks.append(len(local_checks))
        with pytest.raises(EmbeddingRefused, match="scope"):
            await grant.driver.embed((item(text, key),), authorize=lambda: None)
    negative: list[str] = []
    for text, wanted, expected in (("é" * 2049, "input_digest_or_size", 0), (" apple" * 512, "input_tokens", 1)):
        local_checks = []
        with pytest.raises(EmbeddingRefused) as error:
            async with dispatcher.lease_embedding(binding, operation="embed_document", run_id="public-bounds") as grant:
                await grant.driver.embed((item(text, "bounded"),), authorize=partial(local_checks.append, 1))
        assert error.value.code == wanted
        assert len(local_checks) == expected
        assert not leases.active()
        negative.append(wanted)
    revoked: list[bool] = []

    def authorize() -> None:
        revoked.append(True)
        if len(revoked) == 2:
            raise EmbeddingRefused("public_fixture_revoked")  # noqa: EM101 - fixed safe code

    with pytest.raises(EmbeddingRefused, match="public_fixture_revoked"):
        async with dispatcher.lease_embedding(binding, operation="embed_document", run_id="public-revocation") as grant:
            await grant.driver.embed((item("Public synthetic authorization fixture.", "revoked"),), authorize=authorize)
    assert len(revoked) == 2
    assert not leases.active()
    cosines = [math.fsum(a * b for a, b in zip(outputs[2].vector, out.vector, strict=True)) for out in outputs[:2]]
    assert cosines[0] > cosines[1]
    assert _source_hashes() == before
    _verify_imports()
    return {
        "status": "passed",
        "fixture_kind": "public_synthetic",
        "network_namespace": _namespace(),
        "profile_digest": PROFILE.digest,
        "source_sha256": before,
        "driver_calls_completed": 3,
        "source_request_authorizations": checks,
        "negative_controls": [*negative, "escaped_driver", "authority_before_embedding"],
        "vector_digests": [out.vector_digest for out in outputs],
        "tokens": [out.tokens for out in outputs],
        "sanity_cosines": cosines,
        "leases_remaining": len(leases.active()),
    }


def _verify_imports() -> None:
    for name, module in tuple(sys.modules.items()):
        if name == "lychd" or name.startswith("lychd."):
            filename = getattr(module, "__file__", None)
            if filename is not None:
                assert Path(filename).resolve().is_relative_to(_ROOT / "src"), "foreign_candidate_import"


def _namespace() -> str:
    return str(Path("/proc/self/ns/net").readlink())


def _child_main() -> None:
    import resource

    assert len(sys.argv) == 2, "network_namespace"
    assert _namespace() == sys.argv[1], "network_namespace"
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    sys.path[:0] = [str(_ROOT / "src"), str(_ROOT)]

    async def bounded() -> dict[str, Any]:
        async with asyncio.timeout(60):
            return await _driver_probe()

    with redirect_stdout(sys.stderr):
        result = asyncio.run(bounded())
    sys.stdout.write(json.dumps(result, allow_nan=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    _child_main()
