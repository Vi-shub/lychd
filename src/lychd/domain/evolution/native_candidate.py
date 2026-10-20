"""Pure inspection of supplied inactive source bundles; never reads or extracts files.

Declarations bind exact selected bytes. They authenticate neither Git ancestry nor
completeness, compatibility, owner approval, execution containment or activation.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, ValidationError, model_validator

from lychd.domain.artifact_custody import (
    MAX_PAYLOAD_BYTES,
    NATIVE_CANDIDATE_MEDIA_TYPE,
    NATIVE_CANDIDATE_PROFILE,
    Admission,
    ArtifactRecord,
    CustodyCode,
    CustodyPort,
    CustodyRefused,
    CustodyValue,
    Digest,
    InspectionEvidence,
    Label,
    Subject,
    canonical_bytes,
    digest_bytes,
)
from lychd.domain.artifacts import ArtifactRef

MAX_FILES = 128
_CONTROL_LIMIT = 32
_DELETE_CHARACTER = 127
INSPECTOR_REVISION = "native-candidate.canonical-json@1"


def _logical_path(value: str) -> str:
    if (
        value.startswith("/")
        or "\\" in value
        or any(ord(character) < _CONTROL_LIMIT or ord(character) == _DELETE_CHARACTER for character in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or ":" in value
    ):
        raise CustodyRefused(CustodyCode.INPUT)
    return value


LogicalPath = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_logical_path)]
PathSet = Annotated[tuple[LogicalPath, ...], Field(max_length=MAX_FILES)]


def _unique_sorted(paths: tuple[str, ...]) -> None:
    if paths != tuple(sorted(set(paths))):
        raise CustodyRefused(CustodyCode.INPUT)


class CandidateFile(CustodyValue):
    """A regular file's exact bytes and logical mode, without an inode or source path."""

    path: LogicalPath
    kind: Literal["regular"]
    mode: Literal["0644", "0755"]
    size: int = Field(ge=0, le=MAX_PAYLOAD_BYTES)
    digest: Digest
    content_base64: str = Field(max_length=4 * ((MAX_PAYLOAD_BYTES + 2) // 3))

    def source_bytes(self) -> bytes:
        """Decode the declared bytes without consulting any external location."""
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise CustodyRefused(CustodyCode.INPUT) from None
        if (
            base64.b64encode(content).decode("ascii") != self.content_base64
            or len(content) != self.size
            or digest_bytes(content) != self.digest
        ):
            raise CustodyRefused(CustodyCode.INPUT)
        return content

    @model_validator(mode="after")
    def exact_bytes(self) -> CandidateFile:
        self.source_bytes()
        return self


class CandidateDeclaration(CustodyValue):
    """An explicit claim, including a described absence when paths are empty."""

    description: Label
    paths: PathSet

    @model_validator(mode="after")
    def exact_paths(self) -> CandidateDeclaration:
        _unique_sorted(self.paths)
        return self


class CoupledExtension(CandidateDeclaration):
    name: Label


class CandidateClosure(CustodyValue):
    core: CandidateDeclaration
    coupled_extensions: tuple[CoupledExtension, ...] = Field(max_length=32)
    locks: CandidateDeclaration
    configuration: CandidateDeclaration
    schema_state: CandidateDeclaration
    patterns: CandidateDeclaration
    policy: CandidateDeclaration
    history: CandidateDeclaration
    recovery: CandidateDeclaration

    def declarations(self) -> tuple[CandidateDeclaration, ...]:
        return (
            self.core,
            *self.coupled_extensions,
            self.locks,
            self.configuration,
            self.schema_state,
            self.patterns,
            self.policy,
            self.history,
            self.recovery,
        )


class NativeCandidateBundle(CustodyValue):
    """Selected source closure and bounded declarations, never a promotion verdict."""

    profile: Literal["native-candidate-json@1"]
    stage: Literal["inactive"]
    base_commit: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    purpose: Label
    allowed_paths: PathSet = Field(min_length=1)
    selected_paths: PathSet = Field(min_length=1)
    files: tuple[CandidateFile, ...] = Field(min_length=1, max_length=MAX_FILES)
    closure: CandidateClosure

    @model_validator(mode="after")
    def exact_closure(self) -> NativeCandidateBundle:
        _unique_sorted(self.allowed_paths)
        _unique_sorted(self.selected_paths)
        names = tuple(entry.path for entry in self.files)
        extensions = tuple(entry.name for entry in self.closure.coupled_extensions)
        _unique_sorted(extensions)
        declared = {path for declaration in self.closure.declarations() for path in declaration.paths}
        if (
            names != self.selected_paths
            or not set(names) <= set(self.allowed_paths)
            or declared != set(names)
            or not self.closure.core.paths
            or any(not extension.paths for extension in self.closure.coupled_extensions)
            or any(other.startswith(name + "/") for name in names for other in names)
        ):
            raise CustodyRefused(CustodyCode.INPUT)
        return self


def decode_native_candidate(source: ArtifactRef, content: bytes) -> NativeCandidateBundle:
    """Validate exact reference, canonical typed JSON and the selected byte closure."""
    if type(content) is not bytes or len(content) > MAX_PAYLOAD_BYTES:
        raise CustodyRefused(CustodyCode.INPUT)
    if (
        source.media_type != NATIVE_CANDIDATE_MEDIA_TYPE
        or source.size != len(content)
        or source.digest != digest_bytes(content)
    ):
        raise CustodyRefused(CustodyCode.REFERENCE)
    try:
        bundle = NativeCandidateBundle.model_validate_json(content)
    except ValidationError:
        raise CustodyRefused(CustodyCode.INPUT) from None
    if canonical_bytes(bundle) != content:
        raise CustodyRefused(CustodyCode.INPUT)
    return bundle


def inspect_native_candidate(source: ArtifactRef, content: bytes) -> InspectionEvidence:
    bundle = decode_native_candidate(source, content)
    return InspectionEvidence(
        revision=INSPECTOR_REVISION,
        facts=(("file_count", len(bundle.files)), ("source_bytes", sum(entry.size for entry in bundle.files))),
    )


def admit_native_candidate(
    custody: CustodyPort,
    record: ArtifactRecord,
    *,
    subject: Subject,
    purpose: str,
) -> Admission:
    """Inspect retained bytes and seek separate current structural custody admission."""
    if record.binding.profile != NATIVE_CANDIDATE_PROFILE:
        raise CustodyRefused(CustodyCode.INPUT)
    return custody.admit(record, subject=subject, purpose=purpose, inspector=inspect_native_candidate)
