"""Finite Comfy technical probes; these records grant no Composition acceptance."""

import hashlib
import ipaddress
import json
import math
import re
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_JSON_BYTES = 65_536
MAX_OUTPUT_BYTES = 20_000_000
MAX_OUTPUT_OPENINGS = 8
MAX_OUTPUT_FILES = 16
MAX_SUBFOLDER_LENGTH = 256
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SAFE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_FORBIDDEN = re.compile(r"cloud|partner|download|http|credential|password|api.?key|token", re.IGNORECASE)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def json_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def sealed_object(value: str) -> dict[str, Any]:
    if len(value.encode()) > MAX_JSON_BYTES:
        msg = "JSON exceeds probe bound"
        raise ValueError(msg)
    decoded: Any = json.loads(value)
    if not isinstance(decoded, dict) or canonical_json(decoded) != value:
        msg = "Expected a canonical JSON object"
        raise ValueError(msg)
    return cast("dict[str, Any]", decoded)


def safe_component(value: str) -> str:
    if not _SAFE.fullmatch(value) or value in {".", ".."}:
        msg = "Unsafe path component"
        raise ValueError(msg)
    return value


class ProbeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class MutableInput(ProbeModel):
    name: str = Field(min_length=1, max_length=64)
    node_id: str
    input_name: str
    kind: Literal["integer", "number", "text", "boolean"]
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = Field(default=512, ge=1, le=4096)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.kind in {"integer", "number"}:
            if (
                self.minimum is None
                or self.maximum is None
                or not math.isfinite(self.minimum)
                or not math.isfinite(self.maximum)
                or self.minimum > self.maximum
            ):
                msg = "Numeric openings require finite ordered explicit bounds"
                raise ValueError(msg)
        elif self.minimum is not None or self.maximum is not None:
            msg = "Numeric bounds cannot apply to text or boolean openings"
            raise ValueError(msg)
        return self

    def admit(self, value: Any) -> None:
        types = {
            "integer": (int,),
            "number": (int, float),
            "text": (str,),
            "boolean": (bool,),
        }
        if type(value) not in types[self.kind]:
            msg = "Mutable input has the wrong scalar type"
            raise ValueError(msg)
        if self.kind == "text" and len(cast("str", value)) > self.max_length:
            msg = "Mutable text exceeds bound"
            raise ValueError(msg)
        if self.kind in {"integer", "number"} and (
            self.minimum is None
            or self.maximum is None
            or not self.minimum <= cast("int | float", value) <= self.maximum
        ):
            msg = "Mutable numeric input is outside its explicit bounds"
            raise ValueError(msg)


class ModelPin(ProbeModel):
    node_id: str
    input_name: str
    filename: str
    sha256: str

    @model_validator(mode="after")
    def validate_pin(self) -> Self:
        if not _SHA.fullmatch(self.sha256):
            msg = "Invalid model digest"
            raise ValueError(msg)
        safe_component(self.filename)
        return self


class OutputOpening(ProbeModel):
    node_id: str
    kind: Literal["audio", "images", "video"]
    receiving_owner: Literal["riffmaw.music", "atelier.studio"]
    extensions: tuple[str, ...]
    max_files: int = Field(default=1, ge=1, le=8)
    max_bytes: int = Field(default=MAX_OUTPUT_BYTES, ge=1, le=MAX_OUTPUT_BYTES)

    @model_validator(mode="after")
    def validate_owner(self) -> Self:
        expected = "riffmaw.music" if self.kind == "audio" else "atelier.studio"
        permitted = {
            "audio": {".wav", ".flac", ".mp3", ".ogg"},
            "images": {".png", ".jpg", ".webp"},
            "video": {".mp4", ".webm"},
        }
        if self.receiving_owner != expected or not self.extensions or not set(self.extensions) <= permitted[self.kind]:
            msg = "Output kind, owner and extensions must be explicitly compatible"
            raise ValueError(msg)
        return self


class ComfyProfile(ProbeModel):
    """Operator-reviewed closure, sealed independently of mutable request scalars."""

    schema_version: Literal[1] = 1
    endpoint: str
    dialect: Literal["classic"] = "classic"
    deployment_revision: str = Field(min_length=1, max_length=128)
    review_reference: str = Field(min_length=1, max_length=256)
    workflow_json: str
    workflow_sha256: str
    allowed_node_classes: tuple[str, ...]
    models: tuple[ModelPin, ...]
    mutable_inputs: tuple[MutableInput, ...]
    outputs: tuple[OutputOpening, ...]
    client_prompt_id_supported: bool = False

    @model_validator(mode="after")
    def validate_profile(self) -> Self:  # noqa: C901, PLR0912 - one reviewed profile admission boundary
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "http"
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or url.hostname is None
            or not ipaddress.ip_address(url.hostname).is_loopback
            or url.port is None
            or url.port == 0
        ):
            msg = "Endpoint must be literal HTTP loopback with an explicit port and no other URL components"
            raise ValueError(msg)
        graph = sealed_object(self.workflow_json)
        if not graph or json_digest(self.workflow_json) != self.workflow_sha256:
            msg = "Workflow digest mismatch or empty workflow"
            raise ValueError(msg)
        allowed = set(self.allowed_node_classes)
        if not allowed or any(_FORBIDDEN.search(name) for name in allowed):
            msg = "Network, cloud, download and credential node classes are refused"
            raise ValueError(msg)
        for node_id, raw_node in graph.items():
            node = cast("dict[str, Any]", raw_node)
            safe_component(node_id)
            if (
                not isinstance(raw_node, dict)
                or set(node) - {"class_type", "inputs", "_meta"}
                or node.get("class_type") not in allowed
                or not isinstance(node.get("inputs"), dict)
            ):
                msg = "Workflow node does not belong to reviewed closure"
                raise ValueError(msg)
            for name, value in cast("dict[str, Any]", node["inputs"]).items():
                if _FORBIDDEN.search(name) or (isinstance(value, str) and "://" in value):
                    msg = "Credential or remote input is refused"
                    raise ValueError(msg)
        names: set[str] = set()
        slots: set[tuple[str, str]] = set()
        model_slots = {(pin.node_id, pin.input_name) for pin in self.models}
        if len(model_slots) != len(self.models):
            msg = "Duplicate model input slot"
            raise ValueError(msg)
        model_digests: dict[str, str] = {}
        for pin in self.models:
            if pin.filename in model_digests and model_digests[pin.filename] != pin.sha256:
                msg = "Contradictory digest pins for one model filename"
                raise ValueError(msg)
            model_digests[pin.filename] = pin.sha256
        for opening in self.mutable_inputs:
            slot = (opening.node_id, opening.input_name)
            if (
                opening.name in names
                or slot in slots
                or slot in model_slots
                or opening.node_id not in graph
                or opening.input_name not in graph[opening.node_id]["inputs"]
                or _FORBIDDEN.search(opening.input_name)
            ):
                msg = "Mutable openings must be unique existing scalar slots, excluding model and credential inputs"
                raise ValueError(msg)
            opening.admit(graph[opening.node_id]["inputs"][opening.input_name])
            names.add(opening.name)
            slots.add(slot)
        for pin in self.models:
            if pin.node_id not in graph or graph[pin.node_id]["inputs"].get(pin.input_name) != pin.filename:
                msg = "Model pin is not present in workflow"
                raise ValueError(msg)
        if (
            not self.outputs
            or len(self.outputs) > MAX_OUTPUT_OPENINGS
            or sum(item.max_files for item in self.outputs) > MAX_OUTPUT_FILES
            or any(item.node_id not in graph for item in self.outputs)
        ):
            msg = "Expected outputs must name reviewed workflow nodes"
            raise ValueError(msg)
        if len({(item.node_id, item.kind) for item in self.outputs}) != len(self.outputs):
            msg = "Duplicate output opening"
            raise ValueError(msg)
        return self

    @property
    def digest(self) -> str:
        return json_digest(canonical_json(self.model_dump(mode="json")))

    def prompt(self, request: "ComfyRequest") -> dict[str, Any]:
        """Refuse credential or URI inputs before producing the physical request."""
        if request.profile_sha256 != self.digest:
            msg = "Request is bound to a different profile"
            raise ValueError(msg)
        inputs = sealed_object(request.inputs_json)
        if set(inputs) != {opening.name for opening in self.mutable_inputs}:
            msg = "Request must supply exactly the reviewed openings"
            raise ValueError(msg)
        graph = sealed_object(self.workflow_json)
        for opening in self.mutable_inputs:
            opening.admit(inputs[opening.name])
            if _FORBIDDEN.search(opening.input_name) or (
                isinstance(inputs[opening.name], str) and "://" in inputs[opening.name]
            ):
                msg = "Credential or remote input is refused"
                raise ValueError(msg)
            graph[opening.node_id]["inputs"][opening.input_name] = inputs[opening.name]
        return graph


class ComfyRequest(ProbeModel):
    profile_sha256: str
    inputs_json: str

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if not _SHA.fullmatch(self.profile_sha256):
            msg = "Invalid profile digest"
            raise ValueError(msg)
        sealed_object(self.inputs_json)
        return self

    @property
    def digest(self) -> str:
        return json_digest(canonical_json(self.model_dump(mode="json")))


class ComfyArtifact(ProbeModel):
    node_id: str
    kind: Literal["audio", "images", "video"]
    receiving_owner: Literal["riffmaw.music", "atelier.studio"]
    filename: str
    subfolder: str
    storage_type: Literal["output", "temp"]
    local_filename: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        metadata = (self.local_filename, self.sha256, self.size_bytes)
        if any(value is not None for value in metadata) and any(value is None for value in metadata):
            msg = "Fetched artifact metadata must be all present or all absent"
            raise ValueError(msg)
        safe_component(self.node_id)
        safe_component(self.filename)
        if len(self.subfolder) > MAX_SUBFOLDER_LENGTH:
            msg = "Output subfolder exceeds bound"
            raise ValueError(msg)
        if self.subfolder:
            for component in self.subfolder.split("/"):
                safe_component(component)
        if self.local_filename is not None:
            safe_component(self.local_filename)
        if self.sha256 is not None and not _SHA.fullmatch(self.sha256):
            msg = "Invalid artifact digest"
            raise ValueError(msg)
        if self.size_bytes is not None and not 0 < self.size_bytes <= MAX_OUTPUT_BYTES:
            msg = "Artifact size outside bounds"
            raise ValueError(msg)
        return self


class ComfyAttempt(ProbeModel):
    """Technical bookkeeping only; success never conveys semantic approval."""

    schema_version: Literal[1] = 1
    occurrence_id: UUID
    profile_sha256: str
    request_sha256: str
    inputs_json: str
    prompt_sha256: str
    state: Literal[
        "prepared",
        "indeterminate",
        "acknowledged",
        "queued",
        "running",
        "succeeded",
        "failed",
    ]
    provider_id: str | None = None
    artifacts: tuple[ComfyArtifact, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        for digest in (self.profile_sha256, self.request_sha256, self.prompt_sha256):
            if not _SHA.fullmatch(digest):
                msg = "Invalid technical identity digest"
                raise ValueError(msg)
        sealed_object(self.inputs_json)
        if self.provider_id is not None and str(UUID(self.provider_id)) != self.provider_id:
            msg = "Provider identity must be a canonical UUID"
            raise ValueError(msg)
        if len(self.artifacts) > MAX_OUTPUT_FILES:
            msg = "Artifact count exceeds bound"
            raise ValueError(msg)
        return self
