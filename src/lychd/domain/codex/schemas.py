"""Codex decision shapes + pure guards (wave4-design §1.0, §3.4a-c).

`ConsentDecision` is what the ledger's `park` returns to the graph (status + id);
`ConsentView` is the read-model every web surface projects. `censor()` and
`constraints_admit()` are pure, side-effect-free functions: censor creates the
default-deny audit/UI copy while constraints_admit is the fail-closed preauth
constraint check.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, cast
from uuid import UUID

__all__ = [
    "CENSORED_VALUE",
    "ConsentBindingConflict",
    "ConsentDecision",
    "ConsentStatusValue",
    "ConsentView",
    "NativeConsentBinding",
    "censor",
    "constraints_admit",
    "decode_native_binding",
    "require_native_binding",
]

ConsentStatusValue = Literal["pending", "granted", "denied", "expired", "cancelled"]

_IDENTITY_LIMIT = 200

CENSORED_VALUE = "‹censored›"  # noqa: RUF001 - deliberate guillemet marker (design §3.4a)

# The only constraint keys `constraints_admit` understands. ANY other key is fail-closed.
_KNOWN_CONSTRAINT_KEYS: frozenset[str] = frozenset({"args", "path_prefixes"})


class ConsentBindingConflict(ValueError):  # noqa: N818 - named binding conflict contract
    """The retained Consent does not have the exact expected native binding."""


@dataclass(frozen=True, kw_only=True)
class NativeConsentBinding:
    """Immutable identity of one owner-prepared action and its complete review."""

    schema_version: Literal[1] = 1
    owner: str
    action_id: str
    preparation_digest: str
    review_profile: str
    review_profile_revision: str
    review_producer: str
    review_producer_revision: str
    audience_digest: str
    review_digest: str

    def __post_init__(self) -> None:
        """Reject unsupported schema and non-scalar identity before retention."""
        if type(self.schema_version) is not int or self.schema_version != 1:
            message = "Unsupported native Consent binding version."
            raise ValueError(message)
        for value in (
            self.owner,
            self.action_id,
            self.review_profile,
            self.review_profile_revision,
            self.review_producer,
            self.review_producer_revision,
        ):
            if type(value) is not str or not 1 <= len(value) <= _IDENTITY_LIMIT:
                message = "Invalid native Consent identity."
                raise ValueError(message)
        UUID(self.action_id)
        for value in (self.preparation_digest, self.audience_digest, self.review_digest):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                message = "Invalid native Consent digest."
                raise ValueError(message)

    def document(self) -> dict[str, str | int]:
        """Serialize the explicit schema without accepting application payload."""
        return {
            "schema_version": self.schema_version,
            "owner": self.owner,
            "action_id": self.action_id,
            "preparation_digest": self.preparation_digest,
            "review_profile": self.review_profile,
            "review_profile_revision": self.review_profile_revision,
            "review_producer": self.review_producer,
            "review_producer_revision": self.review_producer_revision,
            "audience_digest": self.audience_digest,
            "review_digest": self.review_digest,
        }

    @property
    def digest(self) -> str:
        """Bind exact schema-1 identity using a domain-separated canonical digest."""
        canonical = json.dumps(
            self.document(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(b"lychd.native-consent-binding.v1\n" + canonical.encode("utf-8")).hexdigest()


def decode_native_binding(value: object) -> NativeConsentBinding | None:
    """Decode retained native identity without promoting malformed data to legacy."""
    if isinstance(value, NativeConsentBinding):
        value = value.document()
    if not isinstance(value, dict):
        return None
    try:
        binding = NativeConsentBinding(**cast("dict[str, Any]", value))
        return binding if value.keys() == binding.document().keys() else None
    except (TypeError, ValueError):
        return None


def require_native_binding(view: ConsentView, expected: NativeConsentBinding | None) -> None:
    """Check exact retained identity; this low-level fence grants no authority."""
    if view.native_review_required:
        matches = view.native_binding is not None and expected is not None and view.native_binding == expected
    else:
        matches = view.native_binding is None and expected is None
    if not matches:
        message = "Consent binding changed or is unavailable."
        raise ConsentBindingConflict(message)


@dataclass(frozen=True, kw_only=True)
class ConsentDecision:
    """The ledger's verdict when a run parks: auto-granted, pending, or denied."""

    status: Literal["granted", "pending", "denied"]
    consent_id: str
    preauth_slug: str | None = None


@dataclass(frozen=True, kw_only=True)
class ConsentView:
    """Read-model for one consent row (what every web surface projects)."""

    id: str
    run_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    status: ConsentStatusValue
    decided_by: str | None = None
    decided_at: datetime | None = None
    preauth_slug: str | None = None
    native_review_required: bool = False
    native_binding: NativeConsentBinding | None = None


def censor(payload: dict[str, Any]) -> dict[str, str]:
    """Retain argument names while refusing every value in the consent projection.

    Raw arguments remain available to preauthorization and exact deferred
    execution. The durable consent audit/UI copy is default-deny because a key
    name cannot prove that an arbitrary value is safe to disclose.
    """
    return {str(key): CENSORED_VALUE for key in payload}


def _args_admit(allow: Any, payload: dict[str, Any]) -> bool:
    """Every `{arg: [allowed, ...]}` entry must find the payload arg in its allowlist."""
    if not isinstance(allow, dict):
        return False
    for arg_name, allowed in cast("dict[Any, Any]", allow).items():
        field_name = str(arg_name)
        if not isinstance(allowed, list):
            return False
        if not field_name or field_name not in payload:
            return False
        actual = payload[field_name]
        if not any(_same_json_value(actual, candidate) for candidate in cast("list[Any]", allowed)):
            return False
    return True


def _same_json_value(actual: Any, expected: Any) -> bool:
    """Compare JSON-shaped authority without Python's bool/number coercion."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        actual_object = cast("dict[Any, Any]", actual)
        expected_object = cast("dict[Any, Any]", expected)
        if any(not isinstance(key, str) for key in actual_object) or actual_object.keys() != expected_object.keys():
            return False
        return all(_same_json_value(value, expected_object[key]) for key, value in actual_object.items())
    if isinstance(actual, list):
        actual_array = cast("list[Any]", actual)
        expected_array = cast("list[Any]", expected)
        return len(actual_array) == len(expected_array) and all(
            _same_json_value(value, candidate) for value, candidate in zip(actual_array, expected_array, strict=True)
        )
    if actual is None or isinstance(actual, (bool, int, float, str)):
        return bool(actual == expected)
    return False


def _path_prefixes_admit(prefixes: Any, payload: dict[str, Any]) -> bool:
    """Admit named absolute paths only within their declared lexical roots.

    The constraint shape is ``{payload_field: [allowed_root, ...]}``. Naming the
    field prevents unrelated strings (or a nested path hidden from a shallow scan)
    from being treated as filesystem authority. This is a lexical admission check;
    effect-time code must still resolve symlinks beneath its trusted root.
    """
    if not isinstance(prefixes, dict) or not prefixes:
        return False
    for raw_field, raw_roots in cast("dict[Any, Any]", prefixes).items():
        field_name = str(raw_field)
        candidate_value = payload.get(field_name)
        if not field_name or not isinstance(candidate_value, str):
            return False
        if not isinstance(raw_roots, list) or not raw_roots:
            return False
        candidate = _safe_absolute_path(candidate_value)
        if candidate is None:
            return False
        roots = [_safe_absolute_path(root) if isinstance(root, str) else None for root in cast("list[Any]", raw_roots)]
        if any(root is None for root in roots) or not any(
            candidate.is_relative_to(root) for root in roots if root is not None
        ):
            return False
    return True


def _safe_absolute_path(value: str) -> PurePosixPath | None:
    """Parse one traversal-free absolute lexical path."""
    path = PurePosixPath(value)
    return path if path.is_absolute() and ".." not in path.parts else None


def constraints_admit(constraints: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether ``payload`` satisfies a preauthorization's constraint document.

    Supports ``{"args": {name: [allowed]}, "path_prefixes": {name: [root]}}``.
    Empty constraints admit anything. ANY unrecognized constraint key ⇒ ``False``
    (fail-closed — a constraint we do not understand can never be satisfied).
    """
    for key in constraints:
        if key not in _KNOWN_CONSTRAINT_KEYS:
            return False
    if "args" in constraints and not _args_admit(constraints["args"], payload):
        return False
    return not ("path_prefixes" in constraints and not _path_prefixes_admit(constraints["path_prefixes"], payload))
