"""Bounded official CLI protocols; launch intent is never containment evidence.

The host must provision fresh isolated homes, exclude ambient/project configuration,
resolve executable and roots without symlinks, admit every model request through the
Provider and Portal Gates, and enforce all ADR 09 limits before executing this intent.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import cast

from lychd.domain.delegation.models import (
    DelegatedAgentJobRef,
    DelegatedAgentJobStatus,
    DelegatedAgentProfile,
    DelegatedAgentRequest,
    DelegatedAgentResult,
)

_MAX_PATH_BYTES = 4096
_MAX_MODEL_BYTES = 256
_MAX_PROMPT_BYTES = 1_048_576
_MAX_SESSION_ID_BYTES = 128


class CliRuntimeKind(StrEnum):
    """Explicitly supported noninteractive wire protocols."""

    CODEX = "codex"
    CLAUDE = "claude"


def _require_kind(kind: CliRuntimeKind) -> None:
    if type(kind) is not CliRuntimeKind:
        msg = "An exact supported CLI runtime kind is required."
        raise ValueError(msg)


def reviewed_cli_version(kind: CliRuntimeKind) -> str:
    """Return the exact CLI help revision this launch contract was reviewed against."""
    _require_kind(kind)
    return "0.159.2" if kind is CliRuntimeKind.CODEX else "2.1.296"


@dataclass(frozen=True, slots=True)
class CliLimits:
    """Capture and wall bounds; host resource/Gate budgets are additional requirements."""

    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 16_384
    max_record_bytes: int = 262_144
    wall_seconds: int = 300

    def __post_init__(self) -> None:
        """Reject absent or unbounded capture/deadline limits."""
        for value, maximum in (
            (self.max_stdout_bytes, 1_048_576),
            (self.max_stderr_bytes, 16_384),
            (self.max_record_bytes, self.max_stdout_bytes),
            (self.wall_seconds, 86_400),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                msg = "CLI limits must be positive bounded integers."
                raise ValueError(msg)


def validate_cli_path(value: str) -> PurePosixPath:
    """Check bounded canonical syntax; the host owns real-path verification."""
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or str(path) != value
        or ".." in path.parts
        or not value.isprintable()
        or len(value.encode()) > _MAX_PATH_BYTES
    ):
        msg = "CLI paths must be bounded canonical absolute POSIX paths."
        raise ValueError(msg)
    return path


@dataclass(frozen=True, slots=True)
class CliWorkspace:
    """Syntactic roots only: the host must verify real paths and mount authority."""

    job_root: str
    workspace: str
    scratch: str
    artifacts: str

    def __post_init__(self) -> None:
        """Reject ambiguous or overlapping root intent."""
        root = validate_cli_path(self.job_root)
        children = tuple(validate_cli_path(value) for value in (self.workspace, self.scratch, self.artifacts))
        if root == PurePosixPath("/") or any(root not in child.parents for child in children):
            msg = "Workspace, scratch and artifacts must be strict children of the job root."
            raise ValueError(msg)
        if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(children) for b in children[i + 1 :]):
            msg = "CLI workspace roots must be disjoint."
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class CliLaunchSpec:
    """Immutable command intent passed exclusively to a trusted, refusing-by-default host.

    ``environment`` replaces the inherited environment. It intentionally contains no
    provider credential or auth file. A reviewed Gate bridge is still required; these
    flags and paths alone cannot make either CLI eligible for launch.
    """

    kind: CliRuntimeKind
    request: DelegatedAgentRequest
    job: DelegatedAgentJobRef
    argv: tuple[str, ...]
    stdin: bytes
    workspace: CliWorkspace
    environment: tuple[tuple[str, str], ...]
    limits: CliLimits
    cli_version: str

    @property
    def digest(self) -> str:
        """Bind durable host evidence to the complete admitted command and limits."""
        payload = {
            "kind": self.kind.value,
            "cli_version": self.cli_version,
            "request": self.request.model_dump(mode="json"),
            "job": self.job.model_dump(mode="json"),
            "argv": self.argv,
            "stdin_sha256": sha256(self.stdin).hexdigest(),
            "workspace": asdict(self.workspace),
            "environment": self.environment,
            "limits": asdict(self.limits),
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build_cli_launch_spec(
    kind: CliRuntimeKind,
    *,
    executable: str,
    model: str,
    request: DelegatedAgentRequest,
    job: DelegatedAgentJobRef,
    workspace: CliWorkspace,
    limits: CliLimits,
) -> CliLaunchSpec:
    """Build a literal one-shot command with no resume, shell, or fallback path."""
    _require_kind(kind)
    validate_cli_path(executable)
    if (
        not model
        or model.startswith("-")
        or len(model.encode()) > _MAX_MODEL_BYTES
        or any(not (char.isascii() and (char.isalnum() or char in "._:/-")) for char in model)
    ):
        msg = "An explicit bounded model identifier is required."
        raise ValueError(msg)
    if (
        request.runtime != kind.value
        or job.runtime != kind.value
        or request.request_id != job.request_id
        or request.run_id != job.run_id
        or request.profile != job.profile
    ):
        msg = "CLI request and job identity must agree exactly."
        raise ValueError(msg)
    prompt = request.prompt.encode("utf-8")
    if len(prompt) > _MAX_PROMPT_BYTES:
        msg = "CLI prompt exceeds the byte ceiling."
        raise ValueError(msg)
    if kind is CliRuntimeKind.CODEX:
        sandbox = "workspace-write" if request.profile is DelegatedAgentProfile.CANDIDATE else "read-only"
        argv = (
            executable,
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--config",
            'approval_policy="never"',
            "--sandbox",
            sandbox,
            "--model",
            model,
            "--color",
            "never",
            "--cd",
            workspace.workspace,
            "-",
        )
    else:
        argv = (
            executable,
            "--bare",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            "{}",
            "--setting-sources",
            "",
            "--model",
            model,
        )
    environment = (
        ("HOME", f"{workspace.scratch}/home"),
        ("CODEX_HOME", f"{workspace.scratch}/codex"),
        ("CLAUDE_CONFIG_DIR", f"{workspace.scratch}/claude"),
        ("XDG_CONFIG_HOME", f"{workspace.scratch}/config"),
        ("XDG_CACHE_HOME", f"{workspace.scratch}/cache"),
        ("XDG_DATA_HOME", f"{workspace.scratch}/data"),
        ("TMPDIR", f"{workspace.scratch}/temporary"),
        ("LANG", "C.UTF-8"),
    )
    version = reviewed_cli_version(kind)
    return CliLaunchSpec(kind, request, job, argv, prompt, workspace, environment, limits, version)


class CliProtocolError(ValueError):
    """Bounded output does not establish a complete supported protocol outcome."""


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "Duplicate JSON key."
            raise CliProtocolError(msg)
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    del value
    raise CliProtocolError


def _records(stdout: bytes, limits: CliLimits) -> list[dict[str, object]]:
    if not stdout or not stdout.endswith(b"\n") or len(stdout) > limits.max_stdout_bytes:
        msg = "Missing or oversized CLI output."
        raise CliProtocolError(msg)
    records: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if not line or len(line) > limits.max_record_bytes:
            msg = "Missing or oversized JSONL record."
            raise CliProtocolError(msg)
        value: object = json.loads(line.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise CliProtocolError
        record = cast("dict[str, object]", value)
        if not isinstance(record.get("type"), str):
            msg = "JSONL event requires an object and type."
            raise CliProtocolError(msg)
        records.append(record)
    return records


def _agent_message(item: object) -> str | None:
    if not isinstance(item, dict):
        raise CliProtocolError
    record = cast("dict[str, object]", item)
    if record.get("type") != "agent_message":
        return None
    text = record.get("text")
    if not isinstance(text, str):
        raise CliProtocolError
    return text


def _codex(records: list[dict[str, object]]) -> tuple[bool, str | None]:
    started = False
    turn = False
    output: str | None = None
    for index, record in enumerate(records):
        kind = record["type"]
        if kind == "thread.started" and not started and isinstance(record.get("thread_id"), str):
            started = True
        elif kind == "turn.started" and started and not turn:
            turn = True
        elif kind == "item.completed" and turn:
            text = _agent_message(record.get("item"))
            if text is not None:
                output = text
        elif kind in {"turn.completed", "turn.failed"} and turn and index == len(records) - 1:
            if kind == "turn.completed" and output is None:
                raise CliProtocolError
            return kind == "turn.completed", output
        elif kind not in {"item.started", "item.updated", "error"} or not turn:
            raise CliProtocolError
    raise CliProtocolError


def _claude_session(record: dict[str, object]) -> str:
    session = record.get("session_id")
    if not isinstance(session, str) or not session or not session.isprintable():
        raise CliProtocolError
    if len(session.encode("utf-8")) > _MAX_SESSION_ID_BYTES:
        raise CliProtocolError
    return session


def _claude_message(record: dict[str, object]) -> None:
    """Admit the reviewed bare-print message subset, refusing unknown envelopes."""
    if record["type"] not in {"assistant", "user"} or record.get("error") is not None:
        raise CliProtocolError
    message = record.get("message")
    if not isinstance(message, dict):
        raise CliProtocolError
    fields = cast("dict[str, object]", message)
    if fields.get("role") != record["type"] or not isinstance(fields.get("content"), list):
        raise CliProtocolError


def _claude_result(record: dict[str, object]) -> tuple[bool, str | None]:
    if record["type"] != "result" or type(record.get("is_error")) is not bool:
        raise CliProtocolError
    subtype = record.get("subtype")
    if not isinstance(subtype, str):
        raise CliProtocolError
    if subtype == "success" and record["is_error"] is False:
        output = record.get("result")
        if not isinstance(output, str) or record.get("errors") or record.get("error"):
            raise CliProtocolError
        return True, output
    if (
        subtype
        in {"error_during_execution", "error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}
        and record["is_error"] is True
    ):
        return False, None
    raise CliProtocolError


def _claude(records: list[dict[str, object]]) -> tuple[bool, str | None]:
    """Require one init/session and a single terminal after supported core messages.

    Additional CLI event variants are conservatively LOST until their envelopes and
    ordering have been reviewed. This is a bounded protocol subset, not a permissive
    SDK event collector.
    """
    initial = records[0]
    if initial["type"] != "system" or initial.get("subtype") != "init":
        raise CliProtocolError
    session = _claude_session(initial)
    for record in records[1:]:
        if _claude_session(record) != session:
            raise CliProtocolError
    for record in records[1:-1]:
        _claude_message(record)
    return _claude_result(records[-1])


def parse_cli_output(
    kind: CliRuntimeKind,
    stdout: bytes,
    *,
    job_id: str,
    exit_code: int,
    limits: CliLimits,
) -> DelegatedAgentResult:
    """Interpret provider claims only after the host proves whole-job containment.

    Missing, conflicting, truncated or oversized terminal evidence stays LOST. Raw
    stderr and foreign error strings are deliberately excluded from diagnostics.
    """
    try:
        _require_kind(kind)
        records = _records(stdout, limits)
        succeeded, output = _codex(records) if kind is CliRuntimeKind.CODEX else _claude(records)
        if succeeded and exit_code != 0:
            raise CliProtocolError
        if output is not None:
            output.encode("utf-8")
        return DelegatedAgentResult(
            job_id=job_id,
            status=DelegatedAgentJobStatus.SUCCEEDED if succeeded else DelegatedAgentJobStatus.FAILED,
            output=output if succeeded else None,
            error=None if succeeded else "CLI reported a failed turn.",
        )
    except (ValueError, UnicodeError, RecursionError):
        return DelegatedAgentResult(
            job_id=job_id,
            status=DelegatedAgentJobStatus.LOST,
            error="CLI output did not establish a complete bounded terminal outcome.",
        )
