from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lychd.domain.orchestration.actuator import RuntimePreconditionError
from lychd.domain.orchestration.containment import SoftActivationRecord
from lychd.system.services.runtime_guard import FileSoftActivationGuard


def _record() -> SoftActivationRecord:
    return SoftActivationRecord(
        activation_id="a" * 32,
        target_animator="router",
        target_capability_key="router:chat:model",
        config_generation="sha256:" + "b" * 64,
    )


@pytest.fixture
def guard_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "runtime-guard"
    directory.mkdir(mode=0o700)
    return directory


def test_arm_fsyncs_file_then_directory_before_activation(
    guard_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    real_fsync = os.fsync

    def track_fsync(descriptor: int) -> None:
        events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", track_fsync)
    guard = FileSoftActivationGuard(guard_directory)
    guard.assert_clear()
    guard.arm(_record())
    events.append("activate")
    assert events == ["file", "directory", "activate"]
    assert stat.S_IMODE((guard_directory / "pending.json").stat().st_mode) == 0o600
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        FileSoftActivationGuard(guard_directory).assert_clear()
    guard.clear(_record())
    assert events == ["file", "directory", "activate", "directory"]
    FileSoftActivationGuard(guard_directory).assert_clear()


def test_recovery_inspects_exact_record_before_clearing(guard_directory: Path) -> None:
    FileSoftActivationGuard(guard_directory).arm(_record())
    recovery = FileSoftActivationGuard(guard_directory)
    with pytest.raises(RuntimePreconditionError, match="not owned"):
        recovery.clear(_record())
    assert recovery.pending() == _record()
    recovery.clear(_record())
    assert recovery.pending() is None
    recovery.assert_clear()


@pytest.mark.parametrize("payload", [b"", b"{", b"{}", b"null", b"[]", b"not json", b"x" * 65537])
def test_corrupt_partial_marker_fences_restart(guard_directory: Path, payload: bytes) -> None:
    marker = guard_directory / "pending.json"
    marker.write_bytes(payload)
    marker.chmod(0o600)
    guard = FileSoftActivationGuard(guard_directory)
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        guard.assert_clear()
    with pytest.raises(RuntimePreconditionError, match=r"corrupt|storage limit"):
        guard.pending()
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        guard.arm(_record())
    assert marker.read_bytes() == payload


def test_noncanonical_or_foreign_fields_are_not_recovery_authority(guard_directory: Path) -> None:
    marker = guard_directory / "pending.json"
    payload = _record().model_dump_json().encode()
    marker.write_bytes(payload)
    marker.chmod(0o600)
    with pytest.raises(RuntimePreconditionError, match="noncanonical"):
        FileSoftActivationGuard(guard_directory).pending()
    foreign = payload[:-1] + b',"foreign":true}\n'
    marker.write_bytes(foreign)
    with pytest.raises(RuntimePreconditionError, match="corrupt"):
        FileSoftActivationGuard(guard_directory).pending()
    assert marker.read_bytes() == foreign


@pytest.mark.parametrize("entry", ["unexpected.json", "pending.partial", "old.pending", ".hidden"])
def test_unexpected_entries_fence_startup_and_recovery(guard_directory: Path, entry: str) -> None:
    (guard_directory / entry).touch()
    guard = FileSoftActivationGuard(guard_directory)
    with pytest.raises(RuntimePreconditionError, match="unexpected entries"):
        guard.assert_clear()
    with pytest.raises(RuntimePreconditionError, match="unexpected entries"):
        guard.pending()
    with pytest.raises(RuntimePreconditionError, match="unexpected entries"):
        guard.arm(_record())


def test_missing_directory_is_not_created(tmp_path: Path) -> None:
    directory = tmp_path / "missing"
    with pytest.raises(RuntimePreconditionError, match="existing real directory"):
        FileSoftActivationGuard(directory).assert_clear()
    assert not directory.exists()


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750, 0o500, 0o1700])
def test_directory_mode_is_rejected_without_repair(guard_directory: Path, mode: int) -> None:
    guard_directory.chmod(mode)
    try:
        with pytest.raises(RuntimePreconditionError, match="mode 0700"):
            FileSoftActivationGuard(guard_directory).assert_clear()
        assert stat.S_IMODE(guard_directory.stat().st_mode) == mode
        assert not (guard_directory / "guard.lock").exists()
    finally:
        guard_directory.chmod(0o700)


def test_foreign_directory_owner_is_refused(
    guard_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(RuntimePreconditionError, match="owned by uid"):
        FileSoftActivationGuard(guard_directory).assert_clear()
    assert not (guard_directory / "guard.lock").exists()


@pytest.mark.parametrize("ancestor", [False, True])
def test_symlink_directory_component_is_refused(
    guard_directory: Path,
    tmp_path: Path,
    *,
    ancestor: bool,
) -> None:
    link = tmp_path / "linked"
    link.symlink_to(guard_directory if not ancestor else tmp_path, target_is_directory=True)
    directory = link if not ancestor else link / "runtime-guard"
    with pytest.raises(RuntimePreconditionError, match="existing real directory"):
        FileSoftActivationGuard(directory).assert_clear()
    assert not (guard_directory / "guard.lock").exists()


@pytest.mark.parametrize("entry", ["pending.json", "guard.lock"])
def test_symlink_file_is_preserved(guard_directory: Path, tmp_path: Path, entry: str) -> None:
    target = tmp_path / "untouched"
    target.write_text("foreign content")
    (guard_directory / entry).symlink_to(target)
    guard = FileSoftActivationGuard(guard_directory)
    with pytest.raises(RuntimePreconditionError):
        guard.assert_clear()
    with pytest.raises(RuntimePreconditionError):
        guard.pending()
    assert target.read_text() == "foreign content"
    assert (guard_directory / entry).is_symlink()


@pytest.mark.parametrize("entry", ["pending.json", "guard.lock"])
def test_fifo_file_does_not_block(guard_directory: Path, entry: str) -> None:
    os.mkfifo(guard_directory / entry, mode=0o600)
    with pytest.raises(RuntimePreconditionError, match="regular, single-link"):
        FileSoftActivationGuard(guard_directory).pending()


@pytest.mark.parametrize("entry", ["pending.json", "guard.lock"])
def test_hard_link_file_is_not_adopted(guard_directory: Path, tmp_path: Path, entry: str) -> None:
    target = tmp_path / "original"
    target.write_text(_record().model_dump_json() + "\n")
    target.chmod(0o600)
    os.link(target, guard_directory / entry)
    with pytest.raises(RuntimePreconditionError, match="regular, single-link"):
        FileSoftActivationGuard(guard_directory).pending()
    assert target.stat().st_nlink == 2


def test_duplicate_and_wrong_record_cannot_replace_or_clear(guard_directory: Path) -> None:
    guard = FileSoftActivationGuard(guard_directory)
    record = _record()
    guard.arm(record)
    payload = (guard_directory / "pending.json").read_bytes()
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        guard.arm(record)
    other = record.model_copy(update={"activation_id": "c" * 32})
    with pytest.raises(RuntimePreconditionError, match="changed or is not owned"):
        guard.clear(other)
    assert (guard_directory / "pending.json").read_bytes() == payload
    guard.clear(record)


def test_replaced_inode_with_identical_bytes_cannot_be_cleared(guard_directory: Path, tmp_path: Path) -> None:
    guard = FileSoftActivationGuard(guard_directory)
    guard.arm(_record())
    marker = guard_directory / "pending.json"
    payload = marker.read_bytes()
    marker.rename(tmp_path / "previous-inode")
    marker.write_bytes(payload)
    marker.chmod(0o600)
    with pytest.raises(RuntimePreconditionError, match="changed or is not owned"):
        guard.clear(_record())
    assert marker.read_bytes() == payload


def test_changed_directory_with_identical_record_cannot_be_cleared(guard_directory: Path, tmp_path: Path) -> None:
    guard = FileSoftActivationGuard(guard_directory)
    guard.arm(_record())
    payload = (guard_directory / "pending.json").read_bytes()
    guard_directory.rename(tmp_path / "previous-directory")
    guard_directory.mkdir(mode=0o700)
    marker = guard_directory / "pending.json"
    marker.write_bytes(payload)
    marker.chmod(0o600)
    with pytest.raises(RuntimePreconditionError, match="changed or is not owned"):
        guard.clear(_record())
    assert marker.read_bytes() == payload


@pytest.mark.parametrize("failure_call", [1, 2])
def test_arm_fsync_failure_prevents_effect_and_preserves_blocker(
    guard_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    calls = 0
    events: list[str] = []
    real_fsync = os.fsync

    def fail_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            message = "injected sync failure"
            raise OSError(message)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_fsync)

    def activate() -> None:
        FileSoftActivationGuard(guard_directory).arm(_record())
        events.append("activation must not run")

    with pytest.raises(RuntimePreconditionError, match="injected sync failure"):
        activate()
    assert events == []
    assert (guard_directory / "pending.json").exists()
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        FileSoftActivationGuard(guard_directory).assert_clear()


def test_abrupt_process_exit_preserves_restart_fence(guard_directory: Path) -> None:
    script = """
import os
import sys
from pathlib import Path
from lychd.domain.orchestration.containment import SoftActivationRecord
from lychd.system.services.runtime_guard import FileSoftActivationGuard
record = SoftActivationRecord.model_validate_json(sys.argv[2])
FileSoftActivationGuard(Path(sys.argv[1])).arm(record)
os._exit(37)
"""
    result = subprocess.run(  # noqa: S603 - isolated Python child only publishes a temporary guard record
        [
            sys.executable,
            "-c",
            script,
            str(guard_directory),
            _record().model_dump_json(),
        ],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 37, result.stderr.decode()
    fresh_guard = FileSoftActivationGuard(guard_directory)
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        fresh_guard.assert_clear()
    assert fresh_guard.pending() == _record()


@pytest.mark.parametrize("partial", [False, True])
def test_arm_write_failure_preserves_partial_marker(
    guard_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    partial: bool,
) -> None:
    real_write = os.write
    calls = 0

    def fail_write(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if partial and calls == 1:
            return real_write(descriptor, payload[:5])
        message = "injected write failure"
        raise OSError(message)

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(RuntimePreconditionError, match="injected write failure"):
        FileSoftActivationGuard(guard_directory).arm(_record())
    marker = guard_directory / "pending.json"
    assert marker.read_bytes() == (_record().model_dump_json().encode()[:5] if partial else b"")
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        FileSoftActivationGuard(guard_directory).assert_clear()


def test_short_writes_are_completed(guard_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write

    def short_write(descriptor: int, payload: bytes | memoryview) -> int:
        return real_write(descriptor, payload[:5])

    monkeypatch.setattr(os, "write", short_write)
    guard = FileSoftActivationGuard(guard_directory)
    guard.arm(_record())
    assert guard.pending() == _record()
    guard.clear(_record())


def test_lock_contention_fails_without_new_marker(guard_directory: Path) -> None:
    descriptor = os.open(guard_directory / "guard.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimePreconditionError, match="Cannot safely access"):
            FileSoftActivationGuard(guard_directory).arm(_record())
        assert not (guard_directory / "pending.json").exists()
    finally:
        os.close(descriptor)


def test_clear_sync_failure_restores_a_blocker(guard_directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = FileSoftActivationGuard(guard_directory)
    guard.arm(_record())
    real_fsync = os.fsync
    calls = 0

    def fail_once(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = "clear sync failed"
            raise OSError(message)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(RuntimePreconditionError, match="clear sync failed"):
        guard.clear(_record())
    assert FileSoftActivationGuard(guard_directory).pending() == _record()
    with pytest.raises(RuntimePreconditionError, match="Unresolved soft activation"):
        FileSoftActivationGuard(guard_directory).assert_clear()
