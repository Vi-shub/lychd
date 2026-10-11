"""Descriptor-bound custody on an explicit private root, without activation defaults.

This trusted-host profile serializes operations and pins a lifetime flock. It does
not contain hostile same-UID processes, detect malicious in-place rollback, or
reactivate a clone/restore. Unknown objects are preserved, never garbage-collected.
"""

# ruff: noqa: PTH208, TRY301
# Descriptor-relative listing and locally settled refusal paths are intentional.

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import UUID, uuid4

from pydantic import ValidationError

from lychd.domain.artifact_custody import (
    MAX_HOLDS,
    MAX_METADATA_BYTES,
    MAX_PAYLOAD_BYTES,
    Action,
    Admission,
    AdmissionTarget,
    ArtifactRecord,
    AuthorizationRequest,
    Authorizer,
    Continuity,
    Control,
    CustodyCode,
    CustodyRefused,
    CustodyState,
    CustodyValue,
    CustodyView,
    Decision,
    DeletionResult,
    DeletionTarget,
    FileToken,
    Hold,
    HoldReleaseBinding,
    HoldTarget,
    ImportSpec,
    Inspector,
    MutationTarget,
    PublicationOutcome,
    RecoveryRequired,
    Release,
    ReleaseTarget,
    RetainedHold,
    RetirementState,
    RootBinding,
    Subject,
    Tombstone,
    artifact_id,
    bind_decision,
    canonical_bytes,
    digest_bytes,
    record_digest,
)
from lychd.domain.artifacts import ArtifactRef
from lychd.system.atomic_retirement import AtomicRetirementService, RetirementIdentity
from lychd.system.descriptor_settlement import DescriptorSet
from lychd.system.services import file_publication_recovery as publication_recovery
from lychd.system.services import file_publication_settlement as settlement
from lychd.system.services import file_publication_transaction as publication
from lychd.system.services.file_publication_models import FileIdentity, FilePublication

_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIRECTORY_FLAGS = _READ_FLAGS | os.O_DIRECTORY
_PREFIX_SIZE = 40
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_ROOT_MODE = 0o700
_CONTROL_NAME_PARTS = 3
_MAGIC = b"LYC1"
_LOCK = "custody.lock"
_HEADER = "custody.root"
_ENVELOPE_SUFFIX = ".artifact"


class _Header(CustodyValue):
    binding: RootBinding
    identity: FileToken
    lock: FileToken


def _token(metadata: os.stat_result) -> FileToken:
    return FileToken(device=metadata.st_dev, inode=metadata.st_ino)


def _identity_token(identity: FileIdentity) -> FileToken:
    return FileToken(device=identity.device, inode=identity.inode)


def _frame(value: CustodyValue, payload: bytes = b"") -> bytes:
    metadata = canonical_bytes(value)
    if len(metadata) > MAX_METADATA_BYTES:
        raise CustodyRefused(CustodyCode.INPUT)
    return _MAGIC + len(metadata).to_bytes(4, "big") + hashlib.sha256(metadata).digest() + metadata + payload


def _fingerprint(spec: ImportSpec, subject: Subject, payload_digest: str) -> str:
    return digest_bytes(canonical_bytes(spec) + b"\0" + canonical_bytes(subject) + b"\0" + payload_digest.encode())


def _file_stat(metadata: os.stat_result, *, maximum: int, links: int = 1) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
        or metadata.st_nlink != links
        or not 0 <= metadata.st_size <= maximum
    ):
        raise RecoveryRequired


def _read_exact(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise RecoveryRequired
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _settle_read_descriptors(descriptors: DescriptorSet) -> None:
    primary = sys.exception()
    failures = descriptors.settle()
    if failures:
        peers = (primary, *failures) if primary is not None else failures
        raise RecoveryRequired(failures=peers) from None


def _directory_names(root_fd: int) -> set[str]:
    """Take a fresh directory stream relative to the retained root authority.

    A long-lived directory description can retain a stale enumeration after
    publication on some Linux filesystems. Opening ``.`` refreshes the stream
    without resolving the root pathname again or sharing an enumeration cursor.
    """
    descriptors = DescriptorSet()
    try:
        descriptor = descriptors.add(os.open(".", _DIRECTORY_FLAGS, dir_fd=root_fd))
        return set(os.listdir(descriptor))
    finally:
        _settle_read_descriptors(descriptors)


def _open_root(path: Path) -> int:
    """Check real ancestry; only root-owned sticky shared ancestors may be writable."""
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise RecoveryRequired
    descriptors = DescriptorSet()
    current = descriptors.add(os.open(path.anchor, _DIRECTORY_FLAGS))
    try:
        for component in path.parts[1:]:
            parent = os.fstat(current)
            sticky_shared = parent.st_uid == 0 and bool(parent.st_mode & stat.S_ISVTX)
            if parent.st_uid not in {0, os.getuid()} or (parent.st_mode & 0o022 and not sticky_shared):
                raise RecoveryRequired
            child = descriptors.add(os.open(component, _DIRECTORY_FLAGS, dir_fd=current))
            descriptors.close(current)
            current = child
        metadata = os.fstat(current)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != _PRIVATE_ROOT_MODE:
            raise RecoveryRequired
    except BaseException as exc:
        failures = descriptors.settle()
        if failures:
            raise RecoveryRequired(failures=(exc, *failures)) from None
        raise
    return descriptors.transfer(current)


class LocalArtifactCustody:
    """One lifetime-exclusive local store; construction is through create/open only."""

    def __init__(self, *, root: Path, authorizer: Authorizer) -> None:
        """Allocate inactive state; create/open performs binding and locking."""
        self._root = root
        self._authorizer = authorizer
        self._operation_lock = threading.Lock()
        self._descriptors = DescriptorSet()
        self._root_fd = -1
        self._lock_fd = -1
        self._header: _Header | None = None
        self._closed = False
        self._poisoned = False
        self._views: dict[UUID, CustodyView] = {}
        self._binding: RootBinding | None = None

    @property
    def binding(self) -> RootBinding:
        """Retain this binding outside the root for every subsequent activation."""
        if self._binding is None:
            raise RecoveryRequired
        return self._binding

    @classmethod
    def create(cls, root: Path, *, authorizer: Authorizer) -> Self:
        """Initialize only an explicitly supplied, empty, existing owned 0700 root."""
        store = cls(root=root, authorizer=authorizer)
        try:
            store._root_fd = store._descriptors.add(_open_root(root))
            if _directory_names(store._root_fd):
                raise RecoveryRequired
            store._binding = RootBinding(
                store_id=uuid4(),
                owner_uid=os.getuid(),
                root=_token(os.fstat(store._root_fd)),
            )
            store._lock_fd = store._descriptors.add(
                os.open(_LOCK, _READ_FLAGS | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=store._root_fd)
            )
            os.fchmod(store._lock_fd, 0o600)
            _file_stat(os.fstat(store._lock_fd), maximum=0)
            fcntl.flock(store._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(store._lock_fd)
            settlement.fsync_directory(store._root_fd, path=root)
            lock = _token(os.fstat(store._lock_fd))

            def header(identity: FileIdentity) -> _Header:
                return _Header(binding=store.binding, identity=_identity_token(identity), lock=lock)

            store._header = store._publish(_HEADER, header)
            store._scan()
        except BaseException as exc:
            failures = store._descriptors.settle()
            store._closed = True
            if failures:
                raise RecoveryRequired(failures=(exc, *failures)) from None
            if isinstance(exc, (CustodyRefused, RecoveryRequired)):
                raise
            raise RecoveryRequired(failures=(exc,)) from None
        return store

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        binding: RootBinding,
        authorizer: Authorizer,
        continuity: Continuity,
    ) -> Self:
        """Reopen this exact root only with explicitly asserted ordinary continuity."""
        if continuity is not Continuity.CONTINUOUS or binding.owner_uid != os.getuid():
            raise RecoveryRequired
        store = cls(root=root, authorizer=authorizer)
        store._binding = binding
        try:
            store._root_fd = store._descriptors.add(_open_root(root))
            if _token(os.fstat(store._root_fd)) != binding.root:
                raise RecoveryRequired
            store._lock_fd = store._descriptors.add(os.open(_LOCK, _READ_FLAGS, dir_fd=store._root_fd))
            _file_stat(os.fstat(store._lock_fd), maximum=0)
            try:
                fcntl.flock(store._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CustodyRefused(CustodyCode.LOCKED) from exc
            store._header, _ = store._read_value(_HEADER, _Header)
            store._validate_root()
            store._scan()
        except BaseException as exc:
            failures = store._descriptors.settle()
            store._closed = True
            if failures:
                raise RecoveryRequired(failures=(exc, *failures)) from None
            if isinstance(exc, (CustodyRefused, RecoveryRequired)):
                raise
            raise RecoveryRequired(failures=(exc,)) from None
        return store

    def __enter__(self) -> Self:
        """Return the already activated lifetime-locked store."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release lifetime descriptors on every context exit."""
        self.close()

    def close(self) -> None:
        """Drop each owned descriptor once; a close failure never rolls back records."""
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            failures = self._descriptors.settle()
            if failures:
                raise RecoveryRequired(outcome=PublicationOutcome.COMMITTED, failures=failures)

    @contextmanager
    def _operation(self) -> Generator[None]:
        with self._operation_lock:
            if self._closed:
                raise CustodyRefused(CustodyCode.CLOSED)
            if self._poisoned:
                raise RecoveryRequired
            try:
                self._validate_root()
                self._scan()
                yield
            except RecoveryRequired as exc:
                self._poisoned = exc.outcome is not PublicationOutcome.UNCOMMITTED
                raise
            except ValidationError:
                raise CustodyRefused(CustodyCode.INPUT) from None
            except OSError as exc:
                self._poisoned = True
                raise RecoveryRequired(failures=(exc,)) from None

    def _validate_root(self) -> None:
        descriptors = DescriptorSet()
        try:
            observed = descriptors.add(_open_root(self._root))
            if _token(os.fstat(observed)) != self.binding.root:
                raise RecoveryRequired
            _file_stat(os.fstat(self._lock_fd), maximum=0)
            descriptor = descriptors.add(os.open(_LOCK, _READ_FLAGS, dir_fd=self._root_fd))
            metadata = os.fstat(descriptor)
            _file_stat(metadata, maximum=0)
            header, _ = self._read_value(_HEADER, _Header)
            if (
                header != self._header
                or header.binding != self.binding
                or header.lock != _token(metadata)
                or header.lock != _token(os.fstat(self._lock_fd))
            ):
                raise RecoveryRequired
        finally:
            _settle_read_descriptors(descriptors)

    def _read_value[T: CustodyValue](  # noqa: C901 - one bounded opened-envelope validation chain
        self,
        name: str,
        model: type[T],
        *,
        payload: bool = False,
        expected: ArtifactRecord | None = None,
    ) -> tuple[T, bytes]:
        descriptors = DescriptorSet()
        try:
            descriptor = descriptors.add(os.open(name, _READ_FLAGS, dir_fd=self._root_fd))
            metadata = os.fstat(descriptor)
            maximum = _PREFIX_SIZE + MAX_METADATA_BYTES + (MAX_PAYLOAD_BYTES if model is ArtifactRecord else 0)
            _file_stat(metadata, maximum=maximum)
            prefix = _read_exact(descriptor, _PREFIX_SIZE)
            size = int.from_bytes(prefix[4:8], "big")
            if prefix[:4] != _MAGIC or not 0 < size <= MAX_METADATA_BYTES:
                raise RecoveryRequired
            encoded = _read_exact(descriptor, size)
            if hashlib.sha256(encoded).digest() != prefix[8:]:
                raise RecoveryRequired
            value = model.model_validate_json(encoded)
            if canonical_bytes(value) != encoded:
                raise RecoveryRequired
            identity = value.envelope if isinstance(value, ArtifactRecord) else getattr(value, "identity", None)
            if identity != _token(metadata):
                raise RecoveryRequired
            extra = value.reference.size if isinstance(value, ArtifactRecord) else 0
            if metadata.st_size != _PREFIX_SIZE + size + extra:
                raise RecoveryRequired
            if expected is not None and value != expected:
                raise CustodyRefused(CustodyCode.REFERENCE)
            content = _read_exact(descriptor, extra) if payload else b""
            if payload and isinstance(value, ArtifactRecord) and digest_bytes(content) != value.reference.digest:
                raise RecoveryRequired
            if os.fstat(descriptor) != metadata:
                # Reading can change atime; compare stable custody attributes below.
                after = os.fstat(descriptor)
                _file_stat(after, maximum=maximum)
                if (
                    _token(after) != identity
                    or after.st_size != metadata.st_size
                    or after.st_mtime_ns != metadata.st_mtime_ns
                ):
                    raise RecoveryRequired
        except (OSError, ValueError, ValidationError):
            raise RecoveryRequired from None
        finally:
            _settle_read_descriptors(descriptors)
        return value, content

    def _scan(self) -> None:  # noqa: C901 - closed on-disk name and record grammar
        records: dict[UUID, ArtifactRecord] = {}
        controls: dict[UUID, list[Control]] = {}
        names = _directory_names(self._root_fd)
        for name in sorted(names - {_HEADER, _LOCK}):
            pieces = name.split(".")
            try:
                request_id = UUID(hex=pieces[0])
            except ValueError as exc:
                raise RecoveryRequired from exc
            if pieces[0] != request_id.hex:
                raise RecoveryRequired
            if pieces[1:] == ["artifact"]:
                record, _ = self._read_value(name, ArtifactRecord)
                records[request_id] = record
                self._validate_record(record, request_id)
                continue
            model: type[Control]
            if pieces[1:] == ["admission"]:
                model = Admission
            elif pieces[1:] == ["tombstone"]:
                model = Tombstone
            elif len(pieces) == _CONTROL_NAME_PARTS and pieces[1] in {"hold", "release"}:
                model = Hold if pieces[1] == "hold" else Release
            else:
                raise RecoveryRequired
            control, _ = self._read_value(name, model)
            if isinstance(control, (Hold, Release)) and pieces[2] != control.hold_id.hex:
                raise RecoveryRequired
            self._validate_record(control.record, request_id)
            controls.setdefault(request_id, []).append(control)
        views: dict[UUID, CustodyView] = {}
        for request_id in records.keys() | controls.keys():
            group = controls.get(request_id, [])
            tombstones = [entry for entry in group if isinstance(entry, Tombstone)]
            tombstone = tombstones[0] if tombstones else None
            record = records.get(request_id, tombstone.record if tombstone else None)
            if record is None:
                raise RecoveryRequired
            views[request_id] = self._assemble(record, group)
        self._views = views

    def _validate_record(self, record: ArtifactRecord, request_id: UUID) -> None:
        spec = ImportSpec(
            request_id=record.request_id,
            owner=record.owner,
            media_type="audio/wav",
            classification=record.reference.classification,
            producer_claim=record.producer_claim,
            source_claim=record.source_claim,
        )
        if (
            record.binding != self.binding
            or record.request_id != request_id
            or record.import_fingerprint != _fingerprint(spec, record.importer, record.reference.digest)
        ):
            raise RecoveryRequired

    @staticmethod
    def _assemble(record: ArtifactRecord, group: list[Control]) -> CustodyView:  # noqa: C901 - validate the complete immutable control chain
        admission: Admission | None = None
        tombstone: Tombstone | None = None
        holds: list[Hold] = []
        releases: list[Release] = []
        for entry in group:
            actions: dict[type[Control], Action] = {
                Admission: Action.ADMIT,
                Hold: Action.HOLD,
                Release: Action.RELEASE,
                Tombstone: Action.DELETE,
            }
            action = actions[type(entry)]
            request = entry.decision.request
            if (
                entry.record != record
                or not entry.decision.allowed
                or request.action is not action
                or request.record_digest != record_digest(record)
            ):
                raise RecoveryRequired
            if isinstance(entry, Admission):
                if request.target != AdmissionTarget(evidence=entry.evidence):
                    raise RecoveryRequired
                admission = entry
            elif isinstance(entry, Tombstone):
                tombstone = entry
            elif isinstance(entry, Hold):
                if request.target != HoldTarget(hold_id=entry.hold_id, reason=entry.reason):
                    raise RecoveryRequired
                holds.append(entry)
            elif isinstance(entry, Release):
                releases.append(entry)
        by_hold = {hold.hold_id: hold for hold in holds}
        if len(holds) > MAX_HOLDS or any(
            release.hold_id not in by_hold
            or release.hold_digest != record_digest(by_hold[release.hold_id])
            or release.decision.request.target
            != ReleaseTarget(hold=LocalArtifactCustody._held(by_hold[release.hold_id]), hold_digest=release.hold_digest)
            for release in releases
        ):
            raise RecoveryRequired
        state = CustodyState.DELETED if tombstone else CustodyState.ADMITTED if admission else CustodyState.QUARANTINED
        view = CustodyView(
            record=record,
            state=state,
            admission=admission,
            holds=tuple(holds),
            releases=tuple(releases),
            tombstone=tombstone,
        )
        if tombstone and (
            view.active_holds or tombstone.release_digests != tuple(sorted(record_digest(item) for item in releases))
        ):
            raise RecoveryRequired
        if tombstone and tombstone.decision.request.target != DeletionTarget(
            deletion_id=tombstone.deletion_id, basis=LocalArtifactCustody._release_basis(view)
        ):
            raise RecoveryRequired
        return view

    @staticmethod
    def _held(hold: Hold) -> RetainedHold:
        return RetainedHold(
            hold_id=hold.hold_id,
            reason=hold.reason,
            subject=hold.decision.request.subject,
            purpose=hold.decision.request.purpose,
            created_at=hold.created_at,
            identity=hold.identity,
            record_digest=record_digest(hold.record),
            digest=record_digest(hold),
        )

    @staticmethod
    def _release_basis(view: CustodyView) -> tuple[HoldReleaseBinding, ...]:
        holds = {hold.hold_id: hold for hold in view.holds}
        return tuple(
            HoldReleaseBinding(
                hold_id=release.hold_id,
                hold_digest=record_digest(holds[release.hold_id]),
                release_digest=record_digest(release),
            )
            for release in sorted(view.releases, key=lambda item: item.hold_id.hex)
        )

    def _lookup(self, record: ArtifactRecord) -> CustodyView:
        """Resolve exact trusted metadata without disclosing current object state."""
        current = self._views.get(record.request_id)
        if current is None or record != current.record:
            raise CustodyRefused(CustodyCode.DENIED)
        return current

    @staticmethod
    def _require_live(view: CustodyView) -> None:
        """Expose a deletion refusal only after current authority has been checked."""
        if view.tombstone is not None:
            raise CustodyRefused(CustodyCode.DELETED)

    def _authorize(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        action: Action,
        purpose: str,
        target: MutationTarget | None = None,
    ) -> Decision:
        request = AuthorizationRequest(
            nonce=uuid4(),
            subject=subject,
            binding=self.binding,
            reference=record.reference,
            owner=record.owner,
            record=record,
            importer=record.importer,
            import_fingerprint=record.import_fingerprint,
            record_digest=record_digest(record),
            action=action,
            purpose=purpose,
            target=target,
        )
        return self._decide(request)

    def _decide(self, request: AuthorizationRequest) -> Decision:
        decision = self._authorizer.authorize(request)
        if type(decision) is not Decision or decision.request != request or not decision.allowed:
            raise CustodyRefused(CustodyCode.DENIED)
        return decision

    def _publish[T: CustodyValue](  # noqa: C901 - mutation and post-error settlement remain adjacent
        self,
        name: str,
        builder: Callable[[FileIdentity], T],
        *,
        payload: bytes = b"",
        authorize: Callable[[T], None] | None = None,
    ) -> T:
        staged: FilePublication | None = None
        value: T | None = None
        content_size = 0
        before = _directory_names(self._root_fd)

        def content(identity: FileIdentity) -> bytes:
            nonlocal value, content_size
            value = builder(identity)
            encoded = _frame(value, payload)
            content_size = len(encoded)
            return encoded

        try:
            staged = publication.stage_bytes_file(
                parent_fd=self._root_fd, path=self._root / name, content=content, mode=0o600
            )
            if value is None:
                raise RecoveryRequired
            if authorize is not None:
                authorize(value)
            if not publication.publish_file(staged):
                raise RecoveryRequired
            publication.attest_public_file(staged, owner_uid=os.getuid(), mode=0o600, links=2, size=content_size)
            publication_recovery.remove_staging(staged)
            settlement.fsync_directory(self._root_fd, path=self._root)
        except BaseException as exc:
            if staged is not None and not staged.published:
                try:
                    publication_recovery.remove_staging(staged)
                    settlement.fsync_directory(self._root_fd, path=self._root)
                except BaseException as cleanup:
                    self._poisoned = True
                    raise RecoveryRequired from cleanup
            after = _directory_names(self._root_fd)
            unchanged = before == after and (staged is None or not staged.published)
            if unchanged:
                try:
                    settlement.fsync_directory(self._root_fd, path=self._root)
                except OSError as sync_error:
                    self._poisoned = True
                    raise RecoveryRequired from sync_error
            if unchanged and isinstance(exc, ValidationError):
                raise CustodyRefused(CustodyCode.INPUT) from None
            if unchanged and isinstance(exc, CustodyRefused):
                raise
            self._poisoned = not unchanged
            outcome = PublicationOutcome.UNCOMMITTED if unchanged else PublicationOutcome.RECOVERY_REQUIRED
            raise RecoveryRequired(outcome=outcome) from None
        return value

    def import_bytes(self, content: bytes, spec: ImportSpec, *, subject: Subject, purpose: str) -> CustodyView:
        """Copy exact immutable bytes into quarantine; a retry returns current custody."""
        if type(content) is not bytes or len(content) > MAX_PAYLOAD_BYTES:
            raise CustodyRefused(CustodyCode.INPUT)
        with self._operation():
            payload_digest = digest_bytes(content)
            fingerprint = _fingerprint(spec, subject, payload_digest)
            existing = self._views.get(spec.request_id)
            if existing is not None:
                self._authorize(existing.record, subject=subject, action=Action.IMPORT, purpose=purpose)
                if existing.tombstone:
                    raise CustodyRefused(CustodyCode.DELETED)
                if existing.record.import_fingerprint != fingerprint:
                    raise CustodyRefused(CustodyCode.CONFLICT)
                self._read_value(
                    spec.request_id.hex + _ENVELOPE_SUFFIX, ArtifactRecord, payload=True, expected=existing.record
                )
                self._authorize(existing.record, subject=subject, action=Action.IMPORT, purpose=purpose)
                return existing
            reference = ArtifactRef(
                artifact_id=artifact_id(self.binding.store_id, spec.request_id),
                digest=payload_digest,
                media_type=spec.media_type,
                size=len(content),
                classification=spec.classification,
            )
            now = datetime.now(UTC)

            def build(identity: FileIdentity) -> ArtifactRecord:
                return ArtifactRecord(
                    binding=self.binding,
                    request_id=spec.request_id,
                    reference=reference,
                    owner=spec.owner,
                    importer=subject,
                    producer_claim=spec.producer_claim,
                    source_claim=spec.source_claim,
                    imported_at=now,
                    import_fingerprint=fingerprint,
                    envelope=_identity_token(identity),
                )

            # Initial policy sees the proposed import before any payload staging.
            self._decide(
                AuthorizationRequest(
                    nonce=uuid4(),
                    subject=subject,
                    binding=self.binding,
                    reference=reference,
                    owner=spec.owner,
                    phase="proposal",
                    record=None,
                    proposal=spec,
                    importer=subject,
                    import_fingerprint=fingerprint,
                    record_digest=None,
                    action=Action.IMPORT,
                    purpose=purpose,
                )
            )

            def authorize(record: ArtifactRecord) -> None:
                self._authorize(record, subject=subject, action=Action.IMPORT, purpose=purpose)

            record = self._publish(spec.request_id.hex + _ENVELOPE_SUFFIX, build, payload=content, authorize=authorize)
            return CustodyView(record=record, state=CustodyState.QUARANTINED)

    def _materialize(self, record: ArtifactRecord, *, subject: Subject, purpose: str, action: Action) -> bytes:
        view = self._lookup(record)
        self._authorize(view.record, subject=subject, action=action, purpose=purpose)
        self._require_live(view)
        if action is Action.READ and view.admission is None:
            raise CustodyRefused(CustodyCode.QUARANTINED)
        retained, payload = self._read_value(
            record.request_id.hex + _ENVELOPE_SUFFIX, ArtifactRecord, payload=True, expected=record
        )
        if retained != record:
            raise RecoveryRequired
        self._authorize(record, subject=subject, action=action, purpose=purpose)
        return payload

    def inspect_quarantine(self, record: ArtifactRecord, *, subject: Subject, purpose: str) -> bytes:
        """Disclose retained bytes only under current quarantine-inspection authority."""
        with self._operation():
            return self._materialize(record, subject=subject, purpose=purpose, action=Action.INSPECT)

    def read(self, record: ArtifactRecord, *, subject: Subject, purpose: str) -> bytes:
        """Require the entire record, immutable admission and current read authority."""
        with self._operation():
            return self._materialize(record, subject=subject, purpose=purpose, action=Action.READ)

    def admit(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        purpose: str,
        inspector: Inspector,
    ) -> Admission:
        """Run host-supplied inspector code on retained bytes, then authorize admission."""
        with self._operation():
            view = self._lookup(record)
            payload = self._materialize(record, subject=subject, purpose=purpose, action=Action.INSPECT)
            evidence = inspector(record.reference, payload)
            decision = self._authorize(
                record, subject=subject, action=Action.ADMIT, purpose=purpose, target=AdmissionTarget(evidence=evidence)
            )
            if view.admission is not None:
                if view.admission.evidence != evidence:
                    raise CustodyRefused(CustodyCode.CONFLICT)
                return view.admission

            def build(identity: FileIdentity) -> Admission:
                return Admission(
                    record=record,
                    identity=_identity_token(identity),
                    decision=bind_decision(decision),
                    created_at=datetime.now(UTC),
                    evidence=evidence,
                )

            return self._publish(
                record.request_id.hex + ".admission", build, authorize=lambda _: self._recheck(decision)
            )

    def _recheck(self, decision: Decision) -> None:
        self._decide(decision.request)

    def hold(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        purpose: str,
        hold_id: UUID,
        reason: str,
    ) -> Hold:
        with self._operation():
            view = self._lookup(record)
            target = HoldTarget(hold_id=hold_id, reason=reason)
            decision = self._authorize(view.record, subject=subject, action=Action.HOLD, purpose=purpose, target=target)
            self._require_live(view)
            existing = next((hold for hold in view.holds if hold.hold_id == hold_id), None)
            if existing is not None:
                if existing.reason != reason or existing.decision.request.subject != subject:
                    raise CustodyRefused(CustodyCode.CONFLICT)
                return existing
            if len(view.holds) >= MAX_HOLDS:
                raise CustodyRefused(CustodyCode.HOLD_LIMIT)

            def build(identity: FileIdentity) -> Hold:
                return Hold(
                    record=record,
                    identity=_identity_token(identity),
                    decision=bind_decision(decision),
                    created_at=datetime.now(UTC),
                    hold_id=hold_id,
                    reason=reason,
                )

            return self._publish(
                f"{record.request_id.hex}.hold.{hold_id.hex}", build, authorize=lambda _: self._recheck(decision)
            )

    def release(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        purpose: str,
        hold_id: UUID,
        hold_digest: str,
    ) -> Release:
        with self._operation():
            view = self._lookup(record)
            hold = next((item for item in view.holds if item.hold_id == hold_id), None)
            if hold is None or record_digest(hold) != hold_digest:
                # Missing or mismatched control evidence has no actual policy target.
                # It receives the same opaque refusal as a revoked subject.
                raise CustodyRefused(CustodyCode.DENIED)
            decision = self._authorize(
                view.record,
                subject=subject,
                action=Action.RELEASE,
                purpose=purpose,
                target=ReleaseTarget(hold=self._held(hold), hold_digest=hold_digest),
            )
            self._require_live(view)
            existing = next((item for item in view.releases if item.hold_id == hold_id), None)
            if existing is not None:
                return existing

            def build(identity: FileIdentity) -> Release:
                return Release(
                    record=record,
                    identity=_identity_token(identity),
                    decision=bind_decision(decision),
                    created_at=datetime.now(UTC),
                    hold_id=hold_id,
                    hold_digest=hold_digest,
                )

            return self._publish(
                f"{record.request_id.hex}.release.{hold_id.hex}", build, authorize=lambda _: self._recheck(decision)
            )

    def delete(
        self,
        record: ArtifactRecord,
        *,
        subject: Subject,
        purpose: str,
        deletion_id: UUID,
    ) -> DeletionResult:
        """Commit a permanent tombstone before any exact physical retirement attempt."""
        with self._operation():
            view = self._lookup(record)
            decision = self._authorize(
                record,
                subject=subject,
                action=Action.DELETE,
                purpose=purpose,
                target=DeletionTarget(deletion_id=deletion_id, basis=self._release_basis(view)),
            )
            if view.active_holds:
                raise CustodyRefused(CustodyCode.HELD)
            tombstone = view.tombstone
            if tombstone is not None and tombstone.deletion_id != deletion_id:
                raise CustodyRefused(CustodyCode.CONFLICT)
            if tombstone is None:

                def build(identity: FileIdentity) -> Tombstone:
                    return Tombstone(
                        record=record,
                        identity=_identity_token(identity),
                        decision=bind_decision(decision),
                        created_at=datetime.now(UTC),
                        deletion_id=deletion_id,
                        release_digests=tuple(sorted(record_digest(item) for item in view.releases)),
                    )

                tombstone = self._publish(
                    record.request_id.hex + ".tombstone", build, authorize=lambda _: self._recheck(decision)
                )
            self._recheck(decision)
            return DeletionResult(tombstone=tombstone, retirement=self._retire(record))

    def _retire(self, record: ArtifactRecord) -> RetirementState:
        name = record.request_id.hex + _ENVELOPE_SUFFIX
        if name not in _directory_names(self._root_fd):
            settlement.fsync_directory(self._root_fd, path=self._root)
            return RetirementState.PAYLOAD_ABSENT
        try:
            retained, _ = self._read_value(name, ArtifactRecord)
            if retained != record:
                raise RecoveryRequired
            expected = RetirementIdentity(
                device=record.envelope.device,
                inode=record.envelope.inode,
                owner_uid=self.binding.owner_uid,
                file_type=stat.S_IFREG,
            )
            AtomicRetirementService().retire_file(
                parent_fd=self._root_fd, leaf=name, expected=expected, display_path=self._root / name
            )
            settlement.fsync_directory(self._root_fd, path=self._root)
            if name in _directory_names(self._root_fd):
                raise RecoveryRequired
        except Exception:  # noqa: BLE001 - preserve uncertain retirement outcomes
            # Unsupported native retirement can leave the exact payload intact.
            # Any replacement, private quarantine or uncertain identity is recovery.
            try:
                names = _directory_names(self._root_fd)
                if any(item.startswith(".lychd-") for item in names):
                    raise RecoveryRequired
                if name not in names:
                    self._poisoned = True
                    return RetirementState.RECOVERY_REQUIRED
                retained, _ = self._read_value(name, ArtifactRecord)
                if retained != record:
                    raise RecoveryRequired
            except Exception:  # noqa: BLE001 - preserve uncertain retirement outcomes
                self._poisoned = True
                return RetirementState.RECOVERY_REQUIRED
            return RetirementState.CLEANUP_PENDING
        return RetirementState.PAYLOAD_ABSENT
