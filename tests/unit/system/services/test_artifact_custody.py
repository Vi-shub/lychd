"""Real isolated filesystem custody plus explicitly substituted fault boundaries."""

from __future__ import annotations

import io
import os
import stat
import sys
import threading
import wave
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from lychd.domain.artifact_custody import (
    Action,
    ArtifactRecord,
    AuthorizationRequest,
    Continuity,
    CustodyRefused,
    CustodyState,
    Decision,
    ImportSpec,
    PublicationOutcome,
    RecoveryRequired,
    RetirementState,
    Subject,
    record_digest,
)
from lychd.extensions.builtin.echo.custody import admit_pcm_artifact
from lychd.system.descriptor_settlement import DescriptorSet
from lychd.system.services import artifact_custody as module
from lychd.system.services import file_publication_recovery as recovery
from lychd.system.services import file_publication_transaction as publication
from lychd.system.services.artifact_custody import LocalArtifactCustody
from lychd.system.services.file_publication_models import FilePublication

SUBJECT = Subject(identity="trusted-importer")
OWNER = Subject(identity="retained-owner")
PURPOSE = "bounded custody acceptance"


class Policy:
    def __init__(self) -> None:
        self.denied: set[Action] = set()
        self.calls: list[AuthorizationRequest] = []
        self.hook: Callable[[AuthorizationRequest], None] | None = None

    def authorize(self, request: AuthorizationRequest) -> Decision:
        self.calls.append(request)
        if self.hook:
            self.hook(request)
        return Decision(request=request, allowed=request.action not in self.denied)


def wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x01\x00" * 81)
    return buffer.getvalue()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "custody"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def policy() -> Policy:
    return Policy()


@pytest.fixture
def store(root: Path, policy: Policy) -> Iterator[LocalArtifactCustody]:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        yield store


def imported(store: LocalArtifactCustody) -> ArtifactRecord:
    return store.import_bytes(
        wav(),
        ImportSpec(request_id=uuid4(), owner=OWNER, producer_claim="declared recorder"),
        subject=SUBJECT,
        purpose=PURPOSE,
    ).record


def admitted(store: LocalArtifactCustody) -> ArtifactRecord:
    record = imported(store)
    admit_pcm_artifact(store, record, subject=SUBJECT, purpose=PURPOSE)
    return record


def test_real_wav_quarantine_admission_read_and_restart(root: Path, policy: Policy) -> None:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        binding = store.binding
        record = imported(store)
        with pytest.raises(CustodyRefused, match="quarantined"):
            store.read(record, subject=SUBJECT, purpose=PURPOSE)
        assert store.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE) == wav()
        receipt = admit_pcm_artifact(store, record, subject=SUBJECT, purpose=PURPOSE)
        assert receipt.record == record
        assert dict(receipt.evidence.facts)["frame_count"] == 81
        assert store.read(record, subject=SUBJECT, purpose=PURPOSE) == wav()
        assert policy.calls[0].phase == "proposal"
        assert policy.calls[0].record is None
        assert policy.calls[0].record_digest is None
        assert policy.calls[1].record == record
        assert policy.calls[1].record_digest == record_digest(record)
    with LocalArtifactCustody.open(
        root, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS
    ) as reopened:
        assert reopened.read(record, subject=SUBJECT, purpose=PURPOSE) == wav()
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_id", "urn:lychd:artifact:not-an-id"),
        ("digest", "sha256:" + "a" * 64),
        ("media_type", "audio/ogg"),
        ("size", 1),
        ("classification", "public"),
    ],
)
def test_every_reference_field_is_matched_before_payload(
    store: LocalArtifactCustody,
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = admitted(store)
    forged = record.model_copy(update={"reference": record.reference.model_copy(update={field: value})})
    real = module._read_exact  # pyright: ignore[reportPrivateUsage]
    payload_reads: list[int] = []

    def observe(descriptor: int, size: int) -> bytes:
        if size == len(wav()):
            payload_reads.append(size)
        return real(descriptor, size)

    monkeypatch.setattr(module, "_read_exact", observe)
    with pytest.raises(CustodyRefused, match="denied"):
        store.read(forged, subject=SUBJECT, purpose=PURPOSE)
    assert payload_reads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", Subject(identity="other")),
        ("importer", Subject(identity="other")),
        ("source_claim", "false source"),
        ("producer_claim", "false producer"),
        ("import_fingerprint", "sha256:" + "b" * 64),
    ],
)
def test_full_trusted_record_is_matched(store: LocalArtifactCustody, field: str, value: object) -> None:
    record = admitted(store)
    with pytest.raises(CustodyRefused, match="denied"):
        store.read(record.model_copy(update={field: value}), subject=SUBJECT, purpose=PURPOSE)


@pytest.mark.parametrize("action", [Action.READ, Action.INSPECT])
def test_denied_current_authority_never_reads_payload(
    store: LocalArtifactCustody,
    policy: Policy,
    action: Action,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = admitted(store)
    policy.denied.add(action)
    real = module._read_exact  # pyright: ignore[reportPrivateUsage]
    payload_reads: list[int] = []

    def observe(descriptor: int, size: int) -> bytes:
        if size == record.reference.size:
            payload_reads.append(size)
        return real(descriptor, size)

    monkeypatch.setattr(module, "_read_exact", observe)
    method = store.read if action is Action.READ else store.inspect_quarantine
    with pytest.raises(CustodyRefused, match="denied"):
        method(record, subject=SUBJECT, purpose=PURPOSE)
    assert payload_reads == []


def test_initial_import_denial_has_no_candidate(root: Path, store: LocalArtifactCustody, policy: Policy) -> None:
    before = set(root.iterdir())
    policy.denied.add(Action.IMPORT)
    with pytest.raises(CustodyRefused, match="denied"):
        imported(store)
    assert set(root.iterdir()) == before


def test_retry_returns_current_state_and_conflict_preserves_bytes(store: LocalArtifactCustody) -> None:
    spec = ImportSpec(request_id=uuid4(), owner=OWNER)
    first = store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)
    admit_pcm_artifact(store, first.record, subject=SUBJECT, purpose=PURPOSE)
    replay = store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)
    assert replay.state is CustodyState.ADMITTED
    assert replay.record == first.record
    with pytest.raises(CustodyRefused, match="conflict"):
        store.import_bytes(wav() + b"x", spec, subject=SUBJECT, purpose=PURPOSE)
    with pytest.raises(CustodyRefused, match="conflict"):
        store.import_bytes(wav(), spec.model_copy(update={"source_claim": "changed"}), subject=SUBJECT, purpose=PURPOSE)
    assert store.read(first.record, subject=SUBJECT, purpose=PURPOSE) == wav()


@pytest.mark.parametrize("content", [bytearray(b"data"), memoryview(b"data"), b"x" * (1_048_576 + 1)])
def test_nonimmutable_or_oversized_imports_leave_no_candidates(
    store: LocalArtifactCustody,
    root: Path,
    content: bytes,
) -> None:
    before = set(root.iterdir())
    with pytest.raises(CustodyRefused, match="input"):
        store.import_bytes(content, ImportSpec(request_id=uuid4(), owner=OWNER), subject=SUBJECT, purpose=PURPOSE)
    assert set(root.iterdir()) == before


def test_lifetime_lock_refuses_second_open(store: LocalArtifactCustody, root: Path, policy: Policy) -> None:
    with pytest.raises(CustodyRefused, match="locked"):
        LocalArtifactCustody.open(root, binding=store.binding, authorizer=policy, continuity=Continuity.CONTINUOUS)


@pytest.mark.parametrize("continuity", [Continuity.LOST, Continuity.RESTORED, Continuity.UNCERTAIN])
def test_known_discontinuity_never_activates(root: Path, policy: Policy, continuity: Continuity) -> None:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        binding = store.binding
    with pytest.raises(RecoveryRequired):
        LocalArtifactCustody.open(root, binding=binding, authorizer=policy, continuity=continuity)


@pytest.mark.parametrize(
    "kind", ["stage", "symlink", "fifo", "hardlink", "replacement", "bad_mode", "oversize", "corrupt"]
)
def test_reopen_preserves_unsafe_objects_as_recovery(root: Path, policy: Policy, kind: str) -> None:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        record = imported(store)
        binding = store.binding
    path = root / (record.request_id.hex + ".artifact")
    if kind == "stage":
        (root / ".lychd-create-unexplained").write_bytes(b"foreign")
    elif kind == "hardlink":
        os.link(path, root.parent / "foreign-alias")
    elif kind == "bad_mode":
        path.chmod(0o644)
    elif kind == "corrupt":
        with path.open("r+b") as stream:
            stream.seek(8)
            stream.write(b"!" * 32)
    elif kind == "replacement":
        # Allocate before unlinking the original so its inode cannot be reused.
        replacement = root.parent / "replacement.artifact"
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        replacement.replace(path)
        metadata = path.stat()
        assert (metadata.st_dev, metadata.st_ino) != (record.envelope.device, record.envelope.inode)
    else:
        path.unlink()
        if kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "symlink":
            path.symlink_to(root.parent / "foreign")
        elif kind == "oversize":
            path.write_bytes(b"x" * (1_048_576 + 16_384 + 41))
            path.chmod(0o600)
    before = set(root.iterdir())
    with pytest.raises(RecoveryRequired):
        LocalArtifactCustody.open(root, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS)
    assert set(root.iterdir()) == before


def test_lock_replacement_is_preserved(store: LocalArtifactCustody, root: Path) -> None:
    record = imported(store)
    lock = root / "custody.lock"
    lock.rename(root / "old-lock")
    lock.write_bytes(b"")
    lock.chmod(0o600)
    with pytest.raises(RecoveryRequired):
        store.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE)
    assert lock.is_file()
    assert (root / "old-lock").is_file()


def test_payload_digest_is_checked_only_after_current_authority(
    root: Path,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        record = admitted(store)
        binding = store.binding
    path = root / (record.request_id.hex + ".artifact")
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"!")
    policy.denied.add(Action.READ)
    with LocalArtifactCustody.open(root, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS) as store:
        with pytest.raises(CustodyRefused, match="denied"):
            store.read(record, subject=SUBJECT, purpose=PURPOSE)
        policy.denied.clear()
        with pytest.raises(RecoveryRequired):
            store.read(record, subject=SUBJECT, purpose=PURPOSE)


def test_holds_release_exact_identity_and_tombstone_survives_restart(root: Path, policy: Policy) -> None:
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        record = admitted(store)
        binding = store.binding
        first = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="keep first")
        second = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="keep second")
        with pytest.raises(CustodyRefused, match="denied"):
            store.release(
                record, subject=SUBJECT, purpose=PURPOSE, hold_id=second.hold_id, hold_digest=record_digest(first)
            )
        store.release(record, subject=SUBJECT, purpose=PURPOSE, hold_id=first.hold_id, hold_digest=record_digest(first))
        deletion_id = uuid4()
        with pytest.raises(CustodyRefused, match="held"):
            store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=deletion_id)
        store.release(
            record, subject=SUBJECT, purpose=PURPOSE, hold_id=second.hold_id, hold_digest=record_digest(second)
        )
        result = store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=deletion_id)
        expected = RetirementState.PAYLOAD_ABSENT if sys.platform == "linux" else RetirementState.CLEANUP_PENDING
        assert result.retirement is expected
        assert (root / (record.request_id.hex + ".artifact")).exists() is (expected is RetirementState.CLEANUP_PENDING)
    with LocalArtifactCustody.open(root, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS) as store:
        for method in (store.read, store.inspect_quarantine):
            with pytest.raises(CustodyRefused, match="deleted"):
                method(record, subject=SUBJECT, purpose=PURPOSE)
        spec = ImportSpec(request_id=record.request_id, owner=OWNER, producer_claim=record.producer_claim)
        with pytest.raises(CustodyRefused, match="deleted"):
            store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)
        assert (
            store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=deletion_id).tombstone
            == result.tombstone
        )


def test_lifetime_hold_limit_and_no_reuse(store: LocalArtifactCustody) -> None:
    record = imported(store)
    for index in range(16):
        hold = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=UUID(int=index + 1), reason="keep")
        store.release(record, subject=SUBJECT, purpose=PURPOSE, hold_id=hold.hold_id, hold_digest=record_digest(hold))
    with pytest.raises(CustodyRefused, match="hold_limit"):
        store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="seventeenth")
    with pytest.raises(CustodyRefused, match="conflict"):
        store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=UUID(int=1), reason="different")


@pytest.mark.parametrize("point", ["stage", "file_sync", "link", "after_link", "attest", "cleanup", "namespace_sync"])
def test_publication_faults_have_truthful_outcomes_and_preserve_evidence(  # noqa: C901 - explicit fault-site matrix
    store: LocalArtifactCustody,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    spec = ImportSpec(request_id=uuid4(), owner=OWNER)
    failure = OSError("injected publication failure")
    real_link = os.link
    real_sync = os.fsync

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    def sync(descriptor: int) -> None:
        is_file = stat.S_ISREG(os.fstat(descriptor).st_mode)
        if is_file == (point == "file_sync"):
            raise failure
        real_sync(descriptor)

    def after_link(source: str, target: str, *, src_dir_fd: int, dst_dir_fd: int, follow_symlinks: bool) -> None:
        real_link(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=follow_symlinks)
        raise failure

    if point == "stage":
        monkeypatch.setattr(publication, "stage_bytes_file", fail)
    elif point in {"file_sync", "namespace_sync"}:
        monkeypatch.setattr(os, "fsync", sync)
    elif point == "link":
        monkeypatch.setattr(os, "link", fail)
    elif point == "after_link":
        monkeypatch.setattr(os, "link", after_link)
    elif point == "attest":
        monkeypatch.setattr(publication, "attest_public_file", fail)
    elif point == "cleanup":
        monkeypatch.setattr(recovery, "remove_staging", fail)
    with pytest.raises(RecoveryRequired) as caught:
        store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)
    uncommitted = point in {"stage", "file_sync", "link"}
    assert caught.value.outcome is (
        PublicationOutcome.UNCOMMITTED if uncommitted else PublicationOutcome.RECOVERY_REQUIRED
    )
    assert (root / (spec.request_id.hex + ".artifact")).exists() is (not uncommitted)
    if uncommitted:
        assert {path.name for path in root.iterdir()} == {"custody.root", "custody.lock"}
    else:
        assert any(path.name.startswith(".lychd-create-") for path in root.iterdir())


def test_revoked_during_control_preparation_cannot_publish(
    store: LocalArtifactCustody,
    root: Path,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = imported(store)
    real_stage = publication.stage_bytes_file

    def stage(
        *, parent_fd: int, path: Path, content: bytes | Callable[[publication.FileIdentity], bytes], mode: int
    ) -> FilePublication:
        result = real_stage(parent_fd=parent_fd, path=path, content=content, mode=mode)
        policy.denied.add(Action.ADMIT)
        return result

    monkeypatch.setattr(publication, "stage_bytes_file", stage)
    with pytest.raises(CustodyRefused, match="denied"):
        admit_pcm_artifact(store, record, subject=SUBJECT, purpose=PURPOSE)
    assert not (root / (record.request_id.hex + ".admission")).exists()
    assert not any(path.name.startswith(".lychd-") for path in root.iterdir())


def test_hold_and_delete_are_serialized(
    store: LocalArtifactCustody,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = imported(store)
    entered = threading.Event()
    proceed = threading.Event()
    original = publication.stage_bytes_file

    def stage(
        *, parent_fd: int, path: Path, content: bytes | Callable[[publication.FileIdentity], bytes], mode: int
    ) -> FilePublication:
        result = original(parent_fd=parent_fd, path=path, content=content, mode=mode)
        if ".hold." in path.name:
            entered.set()
            assert proceed.wait(5)
        return result

    monkeypatch.setattr(publication, "stage_bytes_file", stage)
    with ThreadPoolExecutor(max_workers=2) as workers:
        held = workers.submit(store.hold, record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="retain")
        assert entered.wait(5)
        deleted = workers.submit(store.delete, record, subject=SUBJECT, purpose=PURPOSE, deletion_id=uuid4())
        proceed.set()
        assert held.result().reason == "retain"
        with pytest.raises(CustodyRefused, match="held"):
            deleted.result()


def test_close_failure_keeps_committed_record(root: Path, policy: Policy, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalArtifactCustody.create(root, authorizer=policy)
    record = imported(store)
    original = DescriptorSet.settle

    def close(descriptors: DescriptorSet) -> tuple[BaseException, ...]:
        return (*original(descriptors), OSError("closed with error"))

    monkeypatch.setattr(DescriptorSet, "settle", close)
    with pytest.raises(RecoveryRequired) as caught:
        store.close()
    assert caught.value.outcome is PublicationOutcome.COMMITTED
    assert (root / (record.request_id.hex + ".artifact")).exists()


def test_policy_distinguishes_actual_hold_and_release_subjects(store: LocalArtifactCustody, policy: Policy) -> None:
    from lychd.domain.artifact_custody import HoldTarget, ReleaseTarget

    record = imported(store)

    def only_temporary(request: AuthorizationRequest) -> None:
        target = request.target
        if isinstance(target, HoldTarget):
            if target.reason == "legal":
                policy.denied.add(Action.HOLD)
            else:
                policy.denied.discard(Action.HOLD)
        if isinstance(target, ReleaseTarget):
            assert target.hold_digest == target.hold.digest
            if target.hold.reason != "temporary":
                policy.denied.add(Action.RELEASE)

    permanent = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="legal")
    policy.hook = only_temporary
    with pytest.raises(CustodyRefused, match="denied"):
        store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="legal")
    temporary = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="temporary")
    store.release(
        record, subject=SUBJECT, purpose=PURPOSE, hold_id=temporary.hold_id, hold_digest=record_digest(temporary)
    )
    with pytest.raises(CustodyRefused, match="denied"):
        store.release(
            record, subject=SUBJECT, purpose=PURPOSE, hold_id=permanent.hold_id, hold_digest=record_digest(permanent)
        )


def test_invalid_control_input_never_stages_or_exposes_input(
    store: LocalArtifactCustody,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback

    record = imported(store)
    before = set(root.iterdir())
    private_reason = "private-reason-must-not-appear-" * 20

    def never_stage(**kwargs: object) -> None:
        pytest.fail("invalid input reached staging")

    monkeypatch.setattr(publication, "stage_bytes_file", never_stage)
    with pytest.raises(CustodyRefused) as caught:
        store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason=private_reason)
    assert private_reason not in "".join(traceback.format_exception(caught.value))
    assert set(root.iterdir()) == before


@pytest.mark.parametrize("kind", ["absent", "symlink", "world_writable", "unsafe_ancestor"])
def test_create_root_refusal_is_typed_and_preserves_foreign_objects(tmp_path: Path, policy: Policy, kind: str) -> None:
    target = tmp_path / "target"
    if kind == "symlink":
        target.symlink_to(tmp_path)
    elif kind == "world_writable":
        target.mkdir(mode=0o777)
        target.chmod(0o777)
    elif kind == "unsafe_ancestor":
        parent = tmp_path / "unsafe"
        parent.mkdir()
        parent.chmod(0o777)
        target = parent / "target"
        target.mkdir(mode=0o700)
    before = {str(path) for path in tmp_path.rglob("*")}
    with pytest.raises(RecoveryRequired):
        LocalArtifactCustody.create(target, authorizer=policy)
    assert {str(path) for path in tmp_path.rglob("*")} == before


def test_final_namespace_sync_failure_reports_recovery_without_inventing_stage(
    store: LocalArtifactCustody,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sync = os.fsync
    directories = 0

    def sync(descriptor: int) -> None:
        nonlocal directories
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directories += 1
            if directories == 2:
                message = "final namespace sync"
                raise OSError(message)
        real_sync(descriptor)

    monkeypatch.setattr(os, "fsync", sync)
    spec = ImportSpec(request_id=uuid4(), owner=OWNER)
    with pytest.raises(RecoveryRequired) as caught:
        store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)
    assert caught.value.outcome is PublicationOutcome.RECOVERY_REQUIRED
    assert (root / (spec.request_id.hex + ".artifact")).exists()
    assert not any(path.name.startswith(".lychd-") for path in root.iterdir())
    with pytest.raises(RecoveryRequired):
        store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE)


def test_revocation_after_payload_preparation_prevents_disclosure(
    store: LocalArtifactCustody,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = admitted(store)
    real_read = module._read_exact  # pyright: ignore[reportPrivateUsage]

    def revoke(descriptor: int, size: int) -> bytes:
        result = real_read(descriptor, size)
        if size == record.reference.size:
            policy.denied.add(Action.READ)
        return result

    monkeypatch.setattr(module, "_read_exact", revoke)
    with pytest.raises(CustodyRefused, match="denied"):
        store.read(record, subject=SUBJECT, purpose=PURPOSE)


def test_revoke_during_delayed_inspection_cannot_admit(
    store: LocalArtifactCustody,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lychd.extensions.builtin.echo import custody as echo

    record = imported(store)
    entered, proceed = threading.Event(), threading.Event()
    inspect = echo.inspect_pcm_utterance

    def delayed(source: module.ArtifactRef, content: bytes) -> echo.PcmObservation:
        result = inspect(source, content)
        entered.set()
        assert proceed.wait(5)
        return result

    monkeypatch.setattr(echo, "inspect_pcm_utterance", delayed)
    with ThreadPoolExecutor(max_workers=1) as workers:
        admission = workers.submit(admit_pcm_artifact, store, record, subject=SUBJECT, purpose=PURPOSE)
        assert entered.wait(5)
        policy.denied.add(Action.ADMIT)
        proceed.set()
        with pytest.raises(CustodyRefused, match="denied"):
            admission.result()


def test_retirement_handoff_and_namespace_sync_are_required(
    store: LocalArtifactCustody,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substituted retirement checks the handoff; it is not a Linux physical receipt."""
    from lychd.system.atomic_retirement import AtomicRetirementService, RetirementIdentity

    record = imported(store)
    retired: list[str] = []
    real_sync = os.fsync
    synced_after: list[bool] = []

    def retirement(
        self: AtomicRetirementService, *, parent_fd: int, leaf: str, expected: RetirementIdentity, display_path: Path
    ) -> None:
        assert display_path == root / leaf
        assert expected.device == record.envelope.device
        assert expected.inode == record.envelope.inode
        assert expected.owner_uid == os.getuid()
        assert expected.file_type == stat.S_IFREG
        tombstone = root / (record.request_id.hex + ".tombstone")
        assert tombstone.is_file()
        assert not any(path.name.startswith(".lychd-") for path in root.iterdir())
        os.unlink(leaf, dir_fd=parent_fd)
        retired.append(leaf)

    def sync(descriptor: int) -> None:
        if retired and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            synced_after.append(True)
        real_sync(descriptor)

    monkeypatch.setattr(AtomicRetirementService, "retire_file", retirement)
    monkeypatch.setattr(os, "fsync", sync)
    result = store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=uuid4())
    assert result.retirement is RetirementState.PAYLOAD_ABSENT
    assert len(retired) == 1
    assert synced_after


def test_traversal_failure_retains_close_peer(tmp_path: Path, policy: Policy, monkeypatch: pytest.MonkeyPatch) -> None:
    real_settle = DescriptorSet.settle
    peer = OSError("close peer")

    def settle(descriptors: DescriptorSet) -> tuple[BaseException, ...]:
        return (*real_settle(descriptors), peer)

    monkeypatch.setattr(DescriptorSet, "settle", settle)
    with pytest.raises(RecoveryRequired) as caught:
        LocalArtifactCustody.create(tmp_path / "absent", authorizer=policy)
    assert peer in caught.value.failures


@pytest.mark.parametrize("label", ["s", "界", "🦉", "\x00"])
def test_maximal_bounded_metadata_remains_releasable_and_deletable(store: LocalArtifactCustody, label: str) -> None:
    subject = Subject(identity=label * 256)
    owner = Subject(identity=label * 256)
    record = store.import_bytes(
        wav(),
        ImportSpec(request_id=uuid4(), owner=owner, source_claim=label * 256, producer_claim=label * 256),
        subject=subject,
        purpose=label * 256,
    ).record
    for _ in range(16):
        hold = store.hold(record, subject=subject, purpose=label * 256, hold_id=uuid4(), reason=label * 256)
        store.release(
            record, subject=subject, purpose=label * 256, hold_id=hold.hold_id, hold_digest=record_digest(hold)
        )
    result = store.delete(record, subject=subject, purpose=label * 256, deletion_id=uuid4())
    assert len(result.tombstone.release_digests) == 16


@pytest.mark.parametrize("state", [CustodyState.QUARANTINED, CustodyState.ADMITTED, CustodyState.DELETED])
@pytest.mark.parametrize(
    "operation",
    ["read", "inspect", "admit", "hold", "release", "unknown_hold", "wrong_hold_digest", "delete", "import"],
)
def test_revoked_authority_cannot_distinguish_custody_or_control_state(
    root: Path,
    policy: Policy,
    monkeypatch: pytest.MonkeyPatch,
    state: CustodyState,
    operation: str,
) -> None:
    """Real reopening preserves one opaque denial across custody/control states."""
    spec = ImportSpec(request_id=uuid4(), owner=OWNER)
    deletion_id = uuid4()
    with LocalArtifactCustody.create(root, authorizer=policy) as store:
        record = store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE).record
        binding = store.binding
        hold = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="retain")
        store.release(record, subject=SUBJECT, purpose=PURPOSE, hold_id=hold.hold_id, hold_digest=record_digest(hold))
        if state is not CustodyState.QUARANTINED:
            admit_pcm_artifact(store, record, subject=SUBJECT, purpose=PURPOSE)
        if state is CustodyState.DELETED:
            result = store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=deletion_id)
            assert result.retirement is (
                RetirementState.PAYLOAD_ABSENT if sys.platform == "linux" else RetirementState.CLEANUP_PENDING
            )

    before = {path.name: (path.stat().st_ino, path.read_bytes()) for path in root.iterdir()}
    envelope = root / (record.request_id.hex + ".artifact")
    encoded = before.get(envelope.name)
    payload_offset = 40 + int.from_bytes(encoded[1][4:8], "big") if encoded is not None else 0
    payload_reads: list[tuple[int, int]] = []
    real_read = os.read

    def observe_payload_read(descriptor: int, size: int) -> bytes:
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (record.envelope.device, record.envelope.inode):
            offset = os.lseek(descriptor, 0, os.SEEK_CUR)
            if offset + size > payload_offset:
                payload_reads.append((offset, size))
        return real_read(descriptor, size)

    with LocalArtifactCustody.open(root, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS) as store:
        policy.denied.update(Action)
        policy.calls.clear()
        monkeypatch.setattr(os, "read", observe_payload_read)
        calls: dict[str, Callable[[], object]] = {
            "read": lambda: store.read(record, subject=SUBJECT, purpose=PURPOSE),
            "inspect": lambda: store.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE),
            "admit": lambda: admit_pcm_artifact(store, record, subject=SUBJECT, purpose=PURPOSE),
            "hold": lambda: store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="new"),
            "release": lambda: store.release(
                record,
                subject=SUBJECT,
                purpose=PURPOSE,
                hold_id=hold.hold_id,
                hold_digest=record_digest(hold),
            ),
            "unknown_hold": lambda: store.release(
                record,
                subject=SUBJECT,
                purpose=PURPOSE,
                hold_id=uuid4(),
                hold_digest=record_digest(hold),
            ),
            "wrong_hold_digest": lambda: store.release(
                record,
                subject=SUBJECT,
                purpose=PURPOSE,
                hold_id=hold.hold_id,
                hold_digest="sha256:" + "0" * 64,
            ),
            "delete": lambda: store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=deletion_id),
            "import": lambda: store.import_bytes(wav(), spec, subject=SUBJECT, purpose=PURPOSE),
        }
        with pytest.raises(CustodyRefused, match="denied") as caught:
            calls[operation]()
        assert str(caught.value) == "Artifact custody refused: denied."
        if operation in {"unknown_hold", "wrong_hold_digest"}:
            assert policy.calls == []
        else:
            assert len(policy.calls) == 1
            assert policy.calls[0].record == record
        assert payload_reads == []
    after = {path.name: (path.stat().st_ino, path.read_bytes()) for path in root.iterdir()}
    assert after == before
