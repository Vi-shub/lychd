"""Pure bundle refusals and real isolated filesystem custody for inactive candidates."""

from __future__ import annotations

import base64
from pathlib import Path
from uuid import uuid4

import pytest

from lychd.domain.artifact_custody import (
    NATIVE_CANDIDATE_MEDIA_TYPE,
    NATIVE_CANDIDATE_PROFILE,
    Action,
    AuthorizationRequest,
    Continuity,
    CustodyRefused,
    CustodyState,
    Decision,
    ImportSpec,
    RecoveryRequired,
    Subject,
    canonical_bytes,
    digest_bytes,
    record_digest,
)
from lychd.domain.artifacts import ArtifactRef
from lychd.domain.evolution.native_candidate import (
    CandidateClosure,
    CandidateDeclaration,
    CandidateFile,
    CoupledExtension,
    NativeCandidateBundle,
    admit_native_candidate,
    decode_native_candidate,
)
from lychd.system.services.artifact_custody import LocalArtifactCustody

SUBJECT = Subject(identity="candidate-importer")
PURPOSE = "retain inactive candidate"


class Policy:
    def __init__(self) -> None:
        self.denied: set[Action] = set()

    def authorize(self, request: AuthorizationRequest) -> Decision:
        return Decision(request=request, allowed=request.action not in self.denied)


def bundle() -> NativeCandidateBundle:
    content = b"print('inactive')\n"
    declaration = CandidateDeclaration(description="Selected exact Core bytes", paths=("src/core.py",))
    absent = CandidateDeclaration(description="None selected; no corresponding effect is authorized", paths=())
    return NativeCandidateBundle(
        profile=NATIVE_CANDIDATE_PROFILE,
        stage="inactive",
        base_commit="d260ca552fad5c5d9503ea9efc092959dfc57968",
        purpose=PURPOSE,
        allowed_paths=("src/core.py",),
        selected_paths=("src/core.py",),
        files=(
            CandidateFile(
                path="src/core.py",
                kind="regular",
                mode="0644",
                size=len(content),
                digest=digest_bytes(content),
                content_base64=base64.b64encode(content).decode("ascii"),
            ),
        ),
        closure=CandidateClosure(
            core=declaration,
            coupled_extensions=(),
            locks=absent,
            configuration=absent,
            schema_state=absent,
            patterns=absent,
            policy=absent,
            history=absent,
            recovery=absent,
        ),
    )


def source(content: bytes) -> ArtifactRef:
    return ArtifactRef(
        artifact_id="urn:lychd:artifact:test",
        digest=digest_bytes(content),
        media_type=NATIVE_CANDIDATE_MEDIA_TYPE,
        size=len(content),
    )


def spec() -> ImportSpec:
    return ImportSpec(request_id=uuid4(), owner=SUBJECT, media_type=NATIVE_CANDIDATE_MEDIA_TYPE)


def test_canonical_bundle_exact_bytes_and_frozen_declarations() -> None:
    value = bundle()
    content = canonical_bytes(value)
    assert decode_native_candidate(source(content), content) == value
    assert value.files[0].source_bytes() == b"print('inactive')\n"
    with pytest.raises(ValueError, match="frozen"):
        value.purpose = "changed"
    executable = value.model_copy(update={"files": (value.files[0].model_copy(update={"mode": "0755"}),)})
    content = canonical_bytes(executable)
    assert decode_native_candidate(source(content), content).files[0].mode == "0755"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (b'"src/core.py"', b'"../core.py"'),
        (b'"src/core.py"', b'"/src/core.py"'),
        (b'"src/core.py"', b'"src//core.py"'),
        (b'"src/core.py"', b'"src/./core.py"'),
        (b'"src/core.py"', b'"src\\\\core.py"'),
        (b'"regular"', b'"symlink"'),
        (b'"regular"', b'"hardlink"'),
        (b'"0644"', b'"04755"'),
        (b'"inactive"', b'"active"'),
        (b'"size":18', b'"size":19'),
        (b'"files":', b'"filesystem_input":"/tmp/source","files":'),
        (b'"purpose":', b'"purpose":"duplicate","purpose":'),
        (b'"recovery":', b'"unknown_recovery":'),
    ],
)
def test_malformed_candidate_refuses(before: bytes, after: bytes) -> None:
    original = canonical_bytes(bundle())
    content = original.replace(before, after)
    assert content != original
    with pytest.raises(CustodyRefused, match="input"):
        decode_native_candidate(source(content), content)


def test_duplicate_paths_missing_declaration_and_extension_identity_refuse() -> None:
    value = bundle()
    extension = CoupledExtension(name="coupled", description="Selected coupled bytes", paths=value.selected_paths)
    invalid = (
        value.model_copy(update={"files": value.files * 2}),
        value.model_copy(update={"allowed_paths": value.allowed_paths * 2}),
        value.model_copy(update={"selected_paths": value.selected_paths * 2}),
        value.model_copy(update={"closure": value.closure.model_copy(update={"core": value.closure.locks})}),
        value.model_copy(
            update={"closure": value.closure.model_copy(update={"coupled_extensions": (extension, extension)})}
        ),
        value.model_copy(update={"allowed_paths": ("another.py",)}),
        value.model_copy(update={"files": (value.files[0].model_copy(update={"digest": "sha256:" + "0" * 64}),)}),
        value.model_copy(update={"files": (value.files[0].model_copy(update={"content_base64": "not base64"}),)}),
    )
    for malformed in invalid:
        content = canonical_bytes(malformed)
        with pytest.raises(CustodyRefused, match="input"):
            decode_native_candidate(source(content), content)


@pytest.mark.parametrize("field", ["digest", "size", "media_type"])
def test_reference_mismatch_refuses(field: str) -> None:
    content = canonical_bytes(bundle())
    replacement = {"digest": "sha256:" + "0" * 64, "size": 0, "media_type": "audio/wav"}[field]
    with pytest.raises(CustodyRefused, match="reference"):
        decode_native_candidate(source(content).model_copy(update={field: replacement}), content)


def test_noncanonical_and_oversize_bytes_refuse() -> None:
    content = b" " + canonical_bytes(bundle())
    with pytest.raises(CustodyRefused, match="input"):
        decode_native_candidate(source(content), content)
    content = b" " * 1_048_577
    with pytest.raises(CustodyRefused, match="input"):
        decode_native_candidate(source(content), content)


def test_profiles_are_closed_and_wav_remains_default(tmp_path: Path) -> None:
    policy = Policy()
    with LocalArtifactCustody.create(tmp_path, authorizer=policy) as store:
        assert store.binding.profile == "same-host-trusted-bootstrap@1"
        with pytest.raises(CustodyRefused, match="input"):
            store.import_bytes(canonical_bytes(bundle()), spec(), subject=SUBJECT, purpose=PURPOSE)
    other = tmp_path / "candidate"
    other.mkdir(mode=0o700)
    with (
        LocalArtifactCustody.create(other, authorizer=policy, profile=NATIVE_CANDIDATE_PROFILE) as store,
        pytest.raises(CustodyRefused, match="input"),
    ):
        store.import_bytes(b"wav", ImportSpec(request_id=uuid4(), owner=SUBJECT), subject=SUBJECT, purpose=PURPOSE)


def test_candidate_quarantine_admission_authority_hold_tombstone_restart(tmp_path: Path) -> None:
    policy = Policy()
    content = canonical_bytes(bundle())
    request = spec()
    with LocalArtifactCustody.create(tmp_path, authorizer=policy, profile=NATIVE_CANDIDATE_PROFILE) as store:
        binding = store.binding
        record = store.import_bytes(content, request, subject=SUBJECT, purpose=PURPOSE).record
        with pytest.raises(CustodyRefused, match="quarantined"):
            store.read(record, subject=SUBJECT, purpose=PURPOSE)
        policy.denied.add(Action.ADMIT)
        with pytest.raises(CustodyRefused, match="denied"):
            admit_native_candidate(store, record, subject=SUBJECT, purpose=PURPOSE)
        policy.denied.clear()
        receipt = admit_native_candidate(store, record, subject=SUBJECT, purpose=PURPOSE)
        assert dict(receipt.evidence.facts) == {"file_count": 1, "source_bytes": 18}
        assert store.read(record, subject=SUBJECT, purpose=PURPOSE) == content
        forged = record.model_copy(update={"owner": Subject(identity="other")})
        with pytest.raises(CustodyRefused, match="denied"):
            store.read(forged, subject=SUBJECT, purpose=PURPOSE)
        held = store.hold(record, subject=SUBJECT, purpose=PURPOSE, hold_id=uuid4(), reason="review")
        with pytest.raises(CustodyRefused, match="held"):
            store.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=uuid4())
        store.release(record, subject=SUBJECT, purpose=PURPOSE, hold_id=held.hold_id, hold_digest=record_digest(held))
    current = Policy()
    current.denied.add(Action.READ)
    with LocalArtifactCustody.open(
        tmp_path, binding=binding, authorizer=current, continuity=Continuity.CONTINUOUS
    ) as reopened:
        with pytest.raises(CustodyRefused, match="denied"):
            reopened.read(record, subject=SUBJECT, purpose=PURPOSE)
        current.denied.clear()
        assert reopened.import_bytes(content, request, subject=SUBJECT, purpose=PURPOSE).state is CustodyState.ADMITTED
        assert reopened.read(record, subject=SUBJECT, purpose=PURPOSE) == content
        reopened.delete(record, subject=SUBJECT, purpose=PURPOSE, deletion_id=uuid4())
    with LocalArtifactCustody.open(
        tmp_path, binding=binding, authorizer=current, continuity=Continuity.CONTINUOUS
    ) as reopened:
        with pytest.raises(CustodyRefused, match="deleted"):
            reopened.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE)
        with pytest.raises(CustodyRefused, match="deleted"):
            reopened.import_bytes(content, request, subject=SUBJECT, purpose=PURPOSE)


def test_malformed_bytes_remain_quarantined_and_payload_corruption_refuses(tmp_path: Path) -> None:
    policy = Policy()
    with LocalArtifactCustody.create(tmp_path, authorizer=policy, profile=NATIVE_CANDIDATE_PROFILE) as store:
        binding = store.binding
        record = store.import_bytes(b"{}", spec(), subject=SUBJECT, purpose=PURPOSE).record
        with pytest.raises(CustodyRefused, match="input"):
            admit_native_candidate(store, record, subject=SUBJECT, purpose=PURPOSE)
        assert store.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE) == b"{}"
    with (tmp_path / (record.request_id.hex + ".artifact")).open("r+b") as envelope:
        envelope.seek(-1, 2)
        envelope.write(b"!")
    with (
        LocalArtifactCustody.open(
            tmp_path, binding=binding, authorizer=policy, continuity=Continuity.CONTINUOUS
        ) as reopened,
        pytest.raises(RecoveryRequired),
    ):
        reopened.inspect_quarantine(record, subject=SUBJECT, purpose=PURPOSE)
