"""Durable, owner-only containment for runtime-native soft activation.

The small synchronous writes complete before the caller invokes an adapter; no
background writer can outlive cancellation. A marker survives every unsuccessful
arm. Only settled activation or host recovery may clear a previously observed,
unchanged marker. The lock serializes guard operations, not whole transitions.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from lychd.domain.orchestration.actuator import RuntimePreconditionError
from lychd.domain.orchestration.containment import SoftActivationRecord
from lychd.system.descriptor_settlement import FailureLedger, open_directory_path

_PENDING_NAME = "pending.json"
_LOCK_NAME = "guard.lock"
_MAX_RECORD_BYTES = 65536
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class _Observation:
    directory_identity: tuple[int, int]
    marker_identity: tuple[int, int]
    payload: bytes


class FileSoftActivationGuard:
    """Require an existing private directory and preserve unresolved attempts."""

    def __init__(self, directory: Path) -> None:
        """Select storage without creating or repairing any host path."""
        self.directory = directory
        self._observed: _Observation | None = None

    def assert_clear(self) -> None:
        """Refuse every pending, partial, corrupt, or foreign marker."""
        with self._locked_directory() as directory_fd:
            self._require_clear(directory_fd)

    def arm(self, record: SoftActivationRecord) -> None:
        """Publish and fsync an exclusive marker before activation can begin."""
        payload = _encode(record)
        if len(payload) > _MAX_RECORD_BYTES:
            message = "Soft activation record exceeds its storage limit."
            raise RuntimePreconditionError(message)
        with self._locked_directory() as directory_fd:
            self._require_clear(directory_fd)
            metadata = _write_marker(directory_fd, payload)
            self._observed = _Observation(
                directory_identity=_identity(os.fstat(directory_fd)),
                marker_identity=_identity(metadata),
                payload=payload,
            )

    def pending(self) -> SoftActivationRecord | None:
        """Inspect and pin exact recovery evidence without claiming settlement.

        Host recovery must hold its lifecycle authority and prove the recorded
        runtime cold before calling ``clear`` on this same guard instance.
        """
        with self._locked_directory() as directory_fd:
            names = self._entries(directory_fd)
            if _PENDING_NAME not in names:
                self._observed = None
                return None
            with self._read_marker(directory_fd) as (record, observation):
                self._observed = observation
                return record

    def clear(self, record: SoftActivationRecord) -> None:
        """Remove only unchanged evidence from successful arm or inspection."""
        with self._locked_directory() as directory_fd:
            self._entries(directory_fd)
            with self._read_marker(directory_fd) as (current_record, observation):
                if self._observed != observation or current_record != record:
                    message = "Soft activation marker changed or is not owned by this observation."
                    raise RuntimePreconditionError(message)
                current_metadata = os.stat(_PENDING_NAME, dir_fd=directory_fd, follow_symlinks=False)
                if _identity(current_metadata) != observation.marker_identity:
                    message = "Soft activation marker was replaced before settlement."
                    raise RuntimePreconditionError(message)
                os.unlink(_PENDING_NAME, dir_fd=directory_fd)
                try:
                    os.fsync(directory_fd)
                except OSError as exc:
                    try:
                        _write_marker(directory_fd, observation.payload)
                    except OSError as restore_error:
                        message = "Soft activation guard could not persist settlement or restore its blocker."
                        raise ExceptionGroup(message, [exc, restore_error]) from exc
                    raise
                self._observed = None

    @contextmanager
    def _locked_directory(self) -> Generator[int]:
        """Pin the existing directory and serialize descriptor-relative writes."""
        try:
            directory_fd = open_directory_path(
                self.directory,
                failure_ledger=FailureLedger(error_factory=_settlement_error),
                component_error=_component_error,
                unsafe_error=_unsafe_path_error,
                terminal_note="Soft activation guard traversal left its namespace unchanged.",
            )
            try:
                metadata = os.fstat(directory_fd)
                if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE:
                    message = f"Soft activation guard directory must be owned by uid {os.getuid()} with mode 0700."
                    raise RuntimePreconditionError(message)
                lock_fd = os.open(
                    _LOCK_NAME,
                    os.O_RDWR | os.O_CREAT | _FILE_FLAGS,
                    _FILE_MODE,
                    dir_fd=directory_fd,
                )
                try:
                    lock_metadata = os.fstat(lock_fd)
                    _require_private_file(lock_metadata)
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_entry = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
                    if _identity(lock_entry) != _identity(lock_metadata):
                        message = "Soft activation guard lock was replaced."
                        raise RuntimePreconditionError(message)
                    yield directory_fd
                finally:
                    # Closing also releases flock on every exceptional path.
                    os.close(lock_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            message = f"Cannot safely access soft activation guard {self.directory}: {exc}"
            raise RuntimePreconditionError(message) from exc

    def _entries(self, directory_fd: int) -> set[str]:
        names = set(os.listdir(directory_fd))
        if names - {_LOCK_NAME, _PENDING_NAME}:
            message = "Soft activation guard contains unexpected entries; operator recovery is required."
            raise RuntimePreconditionError(message)
        return names

    def _require_clear(self, directory_fd: int) -> None:
        if _PENDING_NAME in self._entries(directory_fd):
            message = "Unresolved soft activation marker requires operator recovery before startup or activation."
            raise RuntimePreconditionError(message)

    @contextmanager
    def _read_marker(self, directory_fd: int) -> Generator[tuple[SoftActivationRecord, _Observation]]:
        marker_fd = os.open(_PENDING_NAME, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory_fd)
        try:
            metadata = os.fstat(marker_fd)
            _require_private_file(metadata)
            payload = os.read(marker_fd, _MAX_RECORD_BYTES + 1)
            if len(payload) > _MAX_RECORD_BYTES:
                message = "Soft activation marker exceeds its storage limit."
                raise RuntimePreconditionError(message)
            try:
                record = SoftActivationRecord.model_validate_json(payload)
            except ValidationError as exc:
                message = "Soft activation marker is corrupt; operator recovery is required."
                raise RuntimePreconditionError(message) from exc
            if payload != _encode(record):
                message = "Soft activation marker has noncanonical contents; operator recovery is required."
                raise RuntimePreconditionError(message)
            observation = _Observation(
                directory_identity=_identity(os.fstat(directory_fd)),
                marker_identity=_identity(metadata),
                payload=payload,
            )
            yield record, observation
        finally:
            os.close(marker_fd)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _encode(record: SoftActivationRecord) -> bytes:
    return record.model_dump_json().encode("utf-8") + b"\n"


def _write_marker(directory_fd: int, payload: bytes) -> os.stat_result:
    marker_fd = os.open(
        _PENDING_NAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
        _FILE_MODE,
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(marker_fd)
        _require_private_file(metadata)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(marker_fd, remaining)
            if written <= 0:
                message = "Soft activation marker write made no progress."
                raise OSError(message)
            remaining = remaining[written:]
        os.fsync(marker_fd)
        os.fsync(directory_fd)
        return metadata
    finally:
        # Never unlink on failure: even a partial write fences restart.
        os.close(marker_fd)


def _require_private_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
        or metadata.st_nlink != 1
    ):
        message = "Soft activation guard files must be regular, single-link, current-owner files with mode 0600."
        raise RuntimePreconditionError(message)


def _component_error(path: Path, error: OSError) -> BaseException:
    return RuntimePreconditionError(f"Soft activation guard requires an existing real directory: {path}: {error}")


def _unsafe_path_error(path: Path) -> BaseException:
    return RuntimePreconditionError(f"Soft activation guard path has an unsafe component: {path}")


def _settlement_error(
    message: str,
    *,
    failures: tuple[BaseException, ...],
    outcome: str,
    verified: bool,
) -> BaseException:
    del failures, outcome, verified
    return RuntimePreconditionError(message)
