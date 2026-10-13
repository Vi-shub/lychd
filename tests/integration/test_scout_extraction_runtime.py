"""Opt-in Linux proof of the fixed Scout extraction boundary.

Uses synthetic public fixtures only. No external contact, daemon or installation.
The explicit profile selects an already reviewed dedicated Python installation,
Bubblewrap and individual loader libraries. Missing prerequisites fail when opted in.
Adversarial worker substitution is private test instrumentation, never a runtime option.
"""

# The proof deliberately challenges private launcher and worker boundaries.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import socket
import sys
import textwrap
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from lychd.domain.scout import extraction as extraction_contract
from lychd.domain.scout.extraction import ExtractionCode, ExtractionError, ExtractionMedia
from lychd.system import scout_worker
from lychd.system.services import scout_extraction

pytestmark = [pytest.mark.integration, pytest.mark.container]


def _runtime() -> scout_extraction.ScoutExtractionRuntime:
    if os.environ.get("LYCHD_REQUIRE_SCOUT_EXTRACTION_PROOF") != "1":
        pytest.skip("Explicit Scout extraction host qualification is not requested.")
    assert sys.platform == "linux", "Scout extraction qualification requires Linux."
    profile = Path(os.environ["LYCHD_SCOUT_EXTRACTION_PROFILE"])
    assert profile.is_absolute()
    assert profile.stat().st_size <= 8192
    config = json.loads(profile.read_text())
    assert set(config) == {"bwrap", "python_root", "libraries"}
    return scout_extraction.ScoutExtractionRuntime(
        bwrap=Path(config["bwrap"]),
        python_root=Path(config["python_root"]),
        libraries=tuple(Path(path) for path in config["libraries"]),
    )


def _worker_copy(path: Path, probe: str, *, limits: bool = True) -> None:
    """Retain the real limit implementation; replace only its guarded entry point."""
    source = Path(scout_worker.__file__).read_text()
    guard = '\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    assert source.count(guard) == 1
    payload = {
        "status": "ok",
        "text": "contained",
        "strategy": scout_worker.PLAIN_STRATEGY,
        "transformations": list(scout_worker.PLAIN_TRANSFORMATIONS),
    }
    response = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    prologue = "_set_resource_limits()\n" if limits else ""
    # Consume the actual protocol before adversarial behavior, avoiding an unrelated broken pipe.
    prologue += "_read_request(sys.stdin.buffer)\n"
    path.write_text(
        source.replace(guard, "\n") + prologue + textwrap.dedent(probe) + f"\nsys.stdout.write({response!r})\n"
    )


def _fingerprint(runtime: scout_extraction.ScoutExtractionRuntime) -> dict[str, object]:
    """Identify the quiescent, explicitly selected closure, not arbitrary host files."""
    root = runtime.python_root.resolve(strict=True)
    digest = sha256()
    total_bytes = 0
    count = 0
    for path in sorted(root.rglob("*")):
        count += 1
        assert count <= 20_000, "Qualification runtime inventory exceeds its bound."
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entry = [relative, "link", str(path.readlink())]
        elif path.is_dir():
            entry = [relative, "directory"]
        else:
            assert path.is_file()
            total_bytes += path.stat().st_size
            assert total_bytes <= 512 * 1024 * 1024, "Qualification runtime exceeds its byte bound."
            entry = [relative, "file", _file_digest(path)]
        digest.update((json.dumps(entry, separators=(",", ":")) + "\n").encode())
    code_paths = (
        Path(scout_worker.__file__),
        Path(scout_extraction.__file__),
        Path(extraction_contract.__file__),
        Path(__file__),
    )
    return {
        "bwrap": {"path": str(runtime.bwrap.resolve()), "digest": _file_digest(runtime.bwrap)},
        "python_root": str(root),
        "python_tree_digest": digest.hexdigest(),
        "python_tree_entries": count,
        "python_tree_bytes": total_bytes,
        "libraries": [
            {"path": str(path.resolve()), "mount_name": path.name, "digest": _file_digest(path)}
            for path in runtime.libraries
        ],
        "code": {path.name: _file_digest(path) for path in code_paths},
    }


def _file_digest(path: Path) -> str:
    before = path.stat()
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ), "Qualification input changed while being read."
    return digest.hexdigest()


def _identity(pid: int) -> tuple[str, str, str, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[19], fields[0], fields[3], fields[1]


def _descendants(pid: int) -> dict[int, str]:
    pending = [pid]
    found: dict[int, str] = {}
    while pending:
        current = pending.pop()
        identity = _identity(current)
        if identity is None or current in found:
            continue
        found[current] = identity[0]
        assert len(found) <= 12, "Unexpected process expansion in the bounded proof."
        try:
            children = Path(f"/proc/{current}/task/{current}/children").read_text()
        except (FileNotFoundError, ProcessLookupError):
            continue
        pending.extend(int(child) for child in children.split())
    return found


def _live(owned: dict[int, str]) -> list[int]:
    return [
        pid
        for pid, started in owned.items()
        if (identity := _identity(pid)) is not None and identity[0] == started and identity[1] != "Z"
    ]


async def _tree_ready(pid: int, minimum: int) -> dict[int, str]:
    async with asyncio.timeout(2):
        while True:
            tree = _descendants(pid)
            statuses = {child: status for child in tree if (status := _identity(child)) is not None}
            parents = {int(status[3]) for status in statuses.values()}
            detached_leaf = any(child not in parents and status[2] == str(child) for child, status in statuses.items())
            if len(tree) >= minimum and detached_leaf:
                return tree
            assert _identity(pid) is not None, "Sandbox exited before the adversarial probe started."
            await asyncio.sleep(0.01)


async def _settlement_case(
    runtime: scout_extraction.ScoutExtractionRuntime, monkeypatch: pytest.MonkeyPatch, *, cancel: bool
) -> None:
    original = asyncio.create_subprocess_exec
    spawned: asyncio.Future[asyncio.subprocess.Process] = asyncio.get_running_loop().create_future()

    async def record(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(*args, **kwargs)
        spawned.set_result(process)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(scout_extraction.asyncio, "create_subprocess_exec", record)
        task = asyncio.create_task(scout_extraction.extract_static_text(b"x", media_type="text/plain", runtime=runtime))
        process = await asyncio.wait_for(asyncio.shield(spawned), 2)
        owned: dict[int, str] = {}
        try:
            # Outer supervisor, namespace reaper, worker and a setsid descendant.
            owned = await _tree_ready(process.pid, 4)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ExtractionError) as error:
                    await task
                assert error.value.code is ExtractionCode.TIMEOUT
            assert process.returncode is not None
            assert not _live(owned), "Extraction returned with live namespace work."
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Recovery of a failing proof is not credited to the product's cleanup.
            for pid in _live(owned):
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


async def _parent_death_case(runtime: scout_extraction.ScoutExtractionRuntime, probe: Path) -> None:
    """Kill the host supervisor itself; only the namespace boundary may settle work."""
    source = f"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, {str(Path(scout_extraction.__file__).parents[3])!r})
from lychd.system.services import scout_extraction as s
s._WORKER_PATH = Path({str(probe)!r})
runtime = s.ScoutExtractionRuntime(Path({str(runtime.bwrap)!r}), Path({str(runtime.python_root)!r}),
                                 tuple(Path(p) for p in {tuple(str(p) for p in runtime.libraries)!r}))
original = asyncio.create_subprocess_exec
async def record(*args, **kwargs):
    child = await original(*args, **kwargs)
    print(child.pid, flush=True)
    return child
asyncio.create_subprocess_exec = record
asyncio.run(s.extract_static_text(b'x', media_type='text/plain', runtime=runtime))
"""
    runner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        source,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={},
        limit=4096,
    )
    owned: dict[int, str] = {}
    try:
        assert runner.stdout is not None
        line = await asyncio.wait_for(runner.stdout.readline(), 2)
        assert line.strip().isdigit()
        owned = await _tree_ready(int(line), 4)
        runner.kill()
        await asyncio.wait_for(runner.wait(), 2)
        async with asyncio.timeout(2):
            while _live(owned):  # noqa: ASYNC110 - Kernel process liveness has no asyncio event source.
                await asyncio.sleep(0.01)
    finally:
        if runner.returncode is None:
            runner.kill()
            await asyncio.wait_for(runner.wait(), 2)
        for pid in _live(owned):
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_scout_extraction_host_boundary(  # noqa: PLR0915 - One bounded qualification lifetime and receipt.
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime()
    checks: list[str] = []
    fingerprint = await asyncio.to_thread(_fingerprint, runtime)
    receipt: dict[str, object] = {
        "status": "failed",
        "profile": "scout-static-extract@1",
        "kernel": platform.release(),
        "python": platform.python_version(),
        "worker_digest": sha256(await asyncio.to_thread(Path(scout_worker.__file__).read_bytes)).hexdigest(),
        "runtime": fingerprint,
        "checks": checks,
    }
    try:
        body = b"<!doctype html><html><head><title>omitted</title></head><body><h1>Public page</h1><p>Hello &amp; welcome.</p><script>omitted</script></body></html>"
        result = await scout_extraction.extract_static_text(body, media_type="text/html", runtime=runtime)
        assert result.text == "Public page\nHello & welcome."
        assert result.source_digest == sha256(body).hexdigest()
        assert result.text_digest == sha256(result.text.encode()).hexdigest()
        assert "Public page" not in repr(result)
        checks.append("real_html_extraction")
        plain = await scout_extraction.extract_static_text(
            b"\xef\xbb\xbfPublic\r\ntext", media_type="text/plain", runtime=runtime
        )
        assert plain.text == "Public\ntext"
        checks.append("real_plain_text_extraction")
        hostile_cases: tuple[tuple[bytes, ExtractionMedia, str], ...] = (
            (b"\xff", "text/plain", "utf8"),
            (b'<!DOCTYPE html SYSTEM "https://invalid.example/dtd">', "text/html", "declaration"),
            (b"<script/>hidden", "text/html", "structure"),
            (b"<div>" * 129, "text/html", "structure"),
            (b"x" * scout_worker.MAX_OUTPUT_BYTES, "text/plain", "output_limit"),
        )
        for hostile, media, code in hostile_cases:
            with pytest.raises(ExtractionError) as error:
                await scout_extraction.extract_static_text(hostile, media_type=media, runtime=runtime)
            assert error.value.code is ExtractionCode.WORKER
            assert error.value.worker_code == code
        checks.append("real_hostile_extraction_refusals")

        secret = tmp_path / "synthetic-host-file"
        secret.write_text("public qualification sentinel")
        probe = tmp_path / "probe.py"
        monkeypatch.setenv("LYCHD_SCOUT_TEST_SENTINEL", "must-not-enter-child")
        with secret.open("rb") as inherited, socket.socket() as listener:
            os.set_inheritable(inherited.fileno(), True)  # noqa: FBT003 - POSIX API takes positional arguments only.
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            _worker_copy(
                probe,
                f"""
                import errno, os, resource, socket
                assert sys.version_info[:3] == {sys.version_info[:3]!r}
                assert "LYCHD_SCOUT_TEST_SENTINEL" not in os.environ
                for descriptor in (*range(3, 64), {inherited.fileno()}):
                    try:
                        os.fstat(descriptor)
                    except OSError as error:
                        assert error.errno == errno.EBADF
                    else:
                        raise AssertionError("inherited descriptor")
                for path in ({str(secret)!r}, "/proc/self/status", "/home", "/etc/resolv.conf"):
                    assert not os.path.exists(path)
                for path in ("/unexpected-file", "/runtime/unexpected-file"):
                    try:
                        with open(path, "w") as destination:
                            destination.write("x")
                    except OSError:
                        pass
                    else:
                        raise AssertionError("writable mount")
                with socket.socket() as connection:
                    connection.settimeout(0.25)
                    try:
                        connection.connect(("127.0.0.1", {port}))
                    except OSError:
                        pass
                    else:
                        raise AssertionError("host network reached")
                try:
                    os.unshare(os.CLONE_NEWUSER)
                except OSError as error:
                    # Bubblewrap can enforce this through the namespace quota (ENOSPC).
                    assert error.errno in (errno.EPERM, errno.ENOSPC)
                else:
                    raise AssertionError("nested user namespace")
                assert resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)
                assert resource.getrlimit(resource.RLIMIT_AS) == (268435456, 268435456)
                assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
                assert resource.getrlimit(resource.RLIMIT_NPROC) == (1, 1)
                try:
                    child = os.fork()
                except OSError as error:
                    assert error.errno == errno.EAGAIN
                else:
                    if child == 0:
                        os._exit(1)
                    os.waitpid(child, 0)
                    raise AssertionError("process expansion")
            """,
            )
            monkeypatch.setattr(scout_extraction, "_WORKER_PATH", probe)
            contained = await scout_extraction.extract_static_text(b"x", media_type="text/plain", runtime=runtime)
            assert contained.text == "contained"
        checks.append("network_filesystem_environment_descriptor_namespace_and_process_denial")

        _worker_copy(
            probe,
            """
            try:
                allocation = bytearray(300 * 1024 * 1024)
            except MemoryError:
                pass
            else:
                raise AssertionError("address-space limit unavailable")
        """,
        )
        await scout_extraction.extract_static_text(b"x", media_type="text/plain", runtime=runtime)
        checks.append("address_space_exhaustion")
        _worker_copy(probe, "while True:\n    pass\n")
        with pytest.raises(ExtractionError) as error:
            await scout_extraction.extract_static_text(b"x", media_type="text/plain", runtime=runtime)
        assert error.value.code is ExtractionCode.CONTAINMENT
        checks.append("cpu_exhaustion")

        for descriptor in (1, 2):
            _worker_copy(probe, f"import os\nwhile True:\n    os.write({descriptor}, b'x' * 4096)\n")
            with pytest.raises(ExtractionError) as error:
                await scout_extraction.extract_static_text(b"x", media_type="text/plain", runtime=runtime)
            assert error.value.code is ExtractionCode.OUTPUT
        checks.append("bounded_stdout_and_stderr_floods")

        _worker_copy(
            probe,
            """
            import os, time
            if os.fork() == 0:
                os.setsid()
            while True:
                time.sleep(1)
        """,
            limits=False,
        )
        await _settlement_case(runtime, monkeypatch, cancel=False)
        checks.append("timeout_settles_new_session_descendant")
        await _settlement_case(runtime, monkeypatch, cancel=True)
        checks.append("cancellation_settles_new_session_descendant")
        await _parent_death_case(runtime, probe)
        checks.append("parent_death_settles_new_session_descendant")
        assert await asyncio.to_thread(_fingerprint, runtime) == fingerprint
        checks.append("runtime_and_code_unchanged")
        receipt["status"] = "passed"
    finally:
        (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
