"""Bounded Linux extraction child; no acquisition, grants, or artifact custody.

Host assembly supplies a reviewed dedicated CPython installation and its explicit
loader closure. These paths are trusted configuration, never request options or
proof of qualification. Unsupported hosts refuse without an uncontained fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from lychd.domain.scout.destination import DestinationError, preflight_destination
from lychd.domain.scout.extraction import (
    ExtractedFeed,
    ExtractedText,
    ExtractionCode,
    ExtractionError,
    ExtractionMedia,
    FeedChannel,
    FeedItem,
    RssMedia,
)
from lychd.system.scout_worker import (
    HTML_STRATEGY,
    HTML_TRANSFORMATIONS,
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    MAX_OUTPUT_BYTES,
    PLAIN_STRATEGY,
    PLAIN_TRANSFORMATIONS,
    REFUSAL_CODES,
    RSS_FIELD_BYTES,
    RSS_MAX_BODY_BYTES,
    RSS_MAX_ELEMENTS,
    RSS_MAX_ITEMS,
    RSS_MEDIA,
    RSS_PROFILE,
    RSS_STRATEGY,
    RSS_TRANSFORMATIONS,
)

_WALL_SECONDS = 5.0
_CLEANUP_SECONDS = 2.0
_STDERR_BYTES = 8192
_PIPE_CHUNK = 4096
_WORKER_REFUSAL_EXIT = 2
_FORBIDDEN_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_WORKER_PATH = Path(__file__).resolve().parents[1] / "scout_worker.py"
_LATE_REAPERS: set[asyncio.Task[None]] = set()
_LIBRARY_NAMES = frozenset(
    {
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libdl.so.2",
        "libgcc_s.so.1",
        "libm.so.6",
        "libpthread.so.0",
        "librt.so.1",
        "libutil.so.1",
    }
)


@dataclass(frozen=True, slots=True)
class ScoutExtractionRuntime:
    """Trusted Linux x86-64 host paths, requiring independent host qualification.

    ``python_root`` is a dedicated interpreter installation containing bin/python3;
    it must contain no operator material or credentials. ``libraries`` names the
    eight individual reviewed runtime files, never broad /lib or /usr mounts.
    No executable, script, environment, or flag can be selected by extraction input.
    """

    bwrap: Path
    python_root: Path
    libraries: tuple[Path, ...]


class ExtractionLifecycle:
    """One invocation's extractor-owned child disposition, without source data.

    ``None`` means termination is unproved. A late reaper cannot upgrade that
    uncertainty into settlement evidence after the extraction has returned.
    The host may read this record, but must never reconstruct or reuse it.
    """

    __slots__ = ("_child", "_entered")

    def __init__(self) -> None:
        """Observe no child until the extractor schedules its first launch."""
        self._child: Literal["not_started", "reaped"] | None = "not_started"
        self._entered = False

    @property
    def child(self) -> Literal["not_started", "reaped"] | None:
        """Return only the disposition observed by this invocation's supervisor."""
        return self._child

    def _begin(self) -> None:
        if self._entered:
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        self._entered = True


def _command(runtime: ScoutExtractionRuntime) -> tuple[str, ...]:
    """Compile one fixed sandbox command; path checks do not attest its binaries."""
    try:
        if sys.platform != "linux" or type(runtime) is not ScoutExtractionRuntime:
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        paths = (runtime.bwrap, runtime.python_root, *runtime.libraries, _WORKER_PATH)
        if any(not path.is_absolute() for path in paths):
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        root = runtime.python_root.resolve(strict=True)
        if root in {Path("/"), Path("/usr"), Path("/usr/local"), Path.home(), Path("/home"), Path("/tmp")}:  # noqa: S108 - This rejects a broad temporary root; it does not create one.
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        if (
            not root.is_dir()
            or not (root / "bin/python3").is_file()
            or not (root / "lib").is_dir()
            or not runtime.bwrap.is_file()
            or not _WORKER_PATH.is_file()
            or len(runtime.libraries) != len(_LIBRARY_NAMES)
            or frozenset(path.name for path in runtime.libraries) != _LIBRARY_NAMES
            or any(not path.is_file() for path in runtime.libraries)
        ):
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        command = [
            str(runtime.bwrap.resolve(strict=True)),
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--disable-userns",
            "--assert-userns-disabled",
            "--clearenv",
            "--new-session",
            "--die-with-parent",
            "--cap-drop",
            "ALL",
            "--ro-bind",
            str(root),
            "/runtime",
            "--ro-bind",
            str(_WORKER_PATH.resolve(strict=True)),
            "/worker.py",
        ]
        for library in sorted(runtime.libraries):
            command.extend(("--ro-bind", str(library.resolve(strict=True)), f"/lib64/{library.name}"))
        command.extend(
            ("--chdir", "/", "--remount-ro", "/", "--", "/runtime/bin/python3", "-I", "-S", "-B", "/worker.py")
        )
    except (OSError, TypeError, AttributeError):
        raise ExtractionError(ExtractionCode.CONTAINMENT) from None
    return tuple(command)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionError(ExtractionCode.PROTOCOL)
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    _ = value
    raise ExtractionError(ExtractionCode.PROTOCOL)


def _payload(record: bytes, exit_code: int) -> dict[str, object]:
    if not record or len(record) > MAX_OUTPUT_BYTES or not record.endswith(b"\n") or record.count(b"\n") != 1:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    try:
        decoded: object = json.loads(
            record.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ExtractionError(ExtractionCode.PROTOCOL) from None
    if not isinstance(decoded, dict):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    payload = cast("dict[str, object]", decoded)
    if set(payload) == {"status", "code"} and payload["status"] == "refused":
        code = payload["code"]
        if type(code) is not str or code not in REFUSAL_CODES or exit_code != _WORKER_REFUSAL_EXIT:
            raise ExtractionError(ExtractionCode.PROTOCOL)
        raise ExtractionError(ExtractionCode.WORKER, worker_code=code)
    return payload


def _result(record: bytes, body: bytes, media_type: ExtractionMedia, exit_code: int) -> ExtractedText:
    payload = _payload(record, exit_code)
    strategy = PLAIN_STRATEGY if media_type == "text/plain" else HTML_STRATEGY
    transformations = PLAIN_TRANSFORMATIONS if media_type == "text/plain" else HTML_TRANSFORMATIONS
    text = payload.get("text")
    if (
        set(payload) != {"status", "text", "strategy", "transformations"}
        or payload["status"] != "ok"
        or exit_code != 0
        or type(text) is not str
        or payload["strategy"] != strategy
        or payload["transformations"] != list(transformations)
    ):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    try:
        encoded = text.encode("utf-8")
    except UnicodeError:
        raise ExtractionError(ExtractionCode.PROTOCOL) from None
    if _FORBIDDEN_CONTROLS.search(text):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    return ExtractedText(
        media_type=media_type,
        text=text,
        source_digest=sha256(body).hexdigest(),
        record_digest=sha256(record).hexdigest(),
        text_digest=sha256(encoded).hexdigest(),
        strategy=strategy,
        transformations=transformations,
    )


def _feed_field(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ExtractionError(ExtractionCode.PROTOCOL) from None
    if size > RSS_FIELD_BYTES[field_name] or _FORBIDDEN_CONTROLS.search(value) or value != value.strip(" \t\r\n"):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    if field_name == "link":
        try:
            preflight_destination(value)
        except DestinationError:
            raise ExtractionError(ExtractionCode.PROTOCOL) from None
    return value


def _feed_fields(value: object, *, item: bool) -> dict[str, object]:
    if type(value) is not dict:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    fields = cast("dict[str, object]", value)
    required = {"title", "link", "summary", "published", "identifier"} if item else {"title", "link", "summary"}
    if set(fields) != required:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    cleaned: dict[str, object] = {}
    for key, field_value in fields.items():
        cleaned[key] = (
            None if key in {"published", "identifier"} and field_value is None else _feed_field(field_value, key)
        )
    if not (cleaned["title"] or cleaned["summary"]) or (not item and not all(cleaned.values())):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    return cleaned


def _feed_result(record: bytes, body: bytes, media_type: RssMedia, exit_code: int) -> ExtractedFeed:
    """Validate the bounded JSON return; Core never parses acquired XML."""
    if type(media_type) is not str or media_type not in RSS_MEDIA:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    payload = _payload(record, exit_code)
    omitted = payload.get("omitted_elements")
    items = payload.get("items")
    if (
        set(payload) != {"status", "strategy", "transformations", "channel", "items", "omitted_elements"}
        or payload["status"] != "ok"
        or exit_code != 0
        or payload["strategy"] != RSS_STRATEGY
        or payload["transformations"] != list(RSS_TRANSFORMATIONS)
        or type(omitted) is not int
        or not 0 <= omitted <= RSS_MAX_ELEMENTS
        or type(items) is not list
        or len(items) > RSS_MAX_ITEMS
    ):
        raise ExtractionError(ExtractionCode.PROTOCOL)
    channel = _feed_fields(payload["channel"], item=False)
    observations: list[FeedItem] = []
    for item_value in cast("list[object]", items):
        item = _feed_fields(item_value, item=True)
        observations.append(
            FeedItem(
                title=cast("str", item["title"]),
                link=cast("str", item["link"]),
                summary=cast("str", item["summary"]),
                published=cast("str | None", item["published"]),
                identifier=cast("str | None", item["identifier"]),
            )
        )
    return ExtractedFeed(
        media_type=media_type,
        channel=FeedChannel(
            cast("str", channel["title"]), cast("str", channel["link"]), cast("str", channel["summary"])
        ),
        items=tuple(observations),
        omitted_elements=omitted,
        source_digest=sha256(body).hexdigest(),
        record_digest=sha256(record).hexdigest(),
        strategy=RSS_STRATEGY,
        transformations=RSS_TRANSFORMATIONS,
    )


async def _read_bounded(reader: asyncio.StreamReader, ceiling: int) -> bytes:
    chunks = bytearray()
    while data := await reader.read(_PIPE_CHUNK):
        if len(chunks) + len(data) > ceiling:
            raise ExtractionError(ExtractionCode.OUTPUT)
        chunks.extend(data)
    return bytes(chunks)


async def _write_request(writer: asyncio.StreamWriter, header: bytes, body: bytes) -> None:
    try:
        writer.write(header)
        for offset in range(0, len(body), _PIPE_CHUNK):
            writer.write(body[offset : offset + _PIPE_CHUNK])
            await writer.drain()
        await writer.drain()
    finally:
        writer.close()


async def _exchange(process: asyncio.subprocess.Process, header: bytes, body: bytes) -> tuple[bytes, int]:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise ExtractionError(ExtractionCode.CONTAINMENT)
    writer = asyncio.create_task(_write_request(process.stdin, header, body))
    stdout = asyncio.create_task(_read_bounded(process.stdout, MAX_OUTPUT_BYTES))
    stderr = asyncio.create_task(_read_bounded(process.stderr, _STDERR_BYTES))
    waiter = asyncio.create_task(process.wait())
    tasks = (writer, stdout, stderr, waiter)
    try:
        await asyncio.gather(*tasks)
        if waiter.result() not in {0, _WORKER_REFUSAL_EXIT} or stderr.result():
            raise ExtractionError(ExtractionCode.CONTAINMENT)
        return stdout.result(), waiter.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _discard(reader: asyncio.StreamReader | None) -> None:
    if reader is not None:
        while await reader.read(_PIPE_CHUNK):
            pass


def _kill(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


async def _reap(process: asyncio.subprocess.Process) -> None:
    await asyncio.gather(process.wait(), _discard(process.stdout), _discard(process.stderr))


async def _reap_late(process: asyncio.subprocess.Process) -> None:
    try:
        async with asyncio.timeout(_CLEANUP_SECONDS):
            await _reap(process)
    except (TimeoutError, OSError):
        # The original caller already received CLEANUP, never a settlement claim.
        return


def _late_reaped(task: asyncio.Task[None]) -> None:
    _LATE_REAPERS.discard(task)
    if not task.cancelled():
        task.exception()


def _kill_late_spawn(spawn: asyncio.Task[asyncio.subprocess.Process]) -> None:
    """Fence a spawn that completes after the original caller received uncertainty."""
    if spawn.cancelled() or spawn.exception() is not None:
        return
    process = spawn.result()
    # Preserve uncertainty; still attempt to observe the owned process below.
    with suppress(OSError):
        _kill(process)
    reaper = asyncio.create_task(_reap_late(process))
    _LATE_REAPERS.add(reaper)
    reaper.add_done_callback(_late_reaped)


async def _cleanup(spawn: asyncio.Task[asyncio.subprocess.Process]) -> Literal["not_started", "reaped"]:
    """Settle the bwrap parent and its mandatory PID namespace, or refuse certainty."""
    try:
        if spawn.done() and not spawn.cancelled() and spawn.exception() is not None:
            return "not_started"
        async with asyncio.timeout(_CLEANUP_SECONDS):
            process = await asyncio.shield(spawn)
            _kill(process)
            await _reap(process)
            return "reaped"
    except (TimeoutError, OSError):
        # Shielded launch may complete later. Retain a kill/reap owner even though
        # the synchronous caller must retain its reservation as uncertain.
        spawn.add_done_callback(_kill_late_spawn)
        raise ExtractionError(ExtractionCode.CLEANUP) from None


async def _finish_cleanup(
    spawn: asyncio.Task[asyncio.subprocess.Process], lifecycle: ExtractionLifecycle | None = None
) -> None:
    if lifecycle is not None:
        # This module's supervisor alone writes the public read-only record.
        lifecycle._child = None  # pyright: ignore[reportPrivateUsage] # noqa: SLF001
    cleanup = asyncio.create_task(_cleanup(spawn))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    disposition = cleanup.result()
    if lifecycle is not None:
        # Publish actual cleanup before propagating cancellation.
        lifecycle._child = disposition  # pyright: ignore[reportPrivateUsage] # noqa: SLF001
    if cancelled:
        raise asyncio.CancelledError


async def extract_static_text(
    body: bytes,
    *,
    media_type: ExtractionMedia,
    runtime: ScoutExtractionRuntime,
    lifecycle: ExtractionLifecycle | None = None,
) -> ExtractedText:
    """Extract one bounded body in the fixed worker, without any automatic retry.

    Cancellation propagates only after bounded cleanup. CLEANUP refusal is an
    uncertain terminal outcome; a caller must not release its work reservation.
    A supplied single-use lifecycle records actual launch/cleanup disposition,
    including refusals before launch and cancellation after proved cleanup.
    This local foundation does not issue an Extract grant or bind a web attempt.
    """
    observation = ExtractionLifecycle() if lifecycle is None else lifecycle
    if type(observation) is not ExtractionLifecycle:
        raise ExtractionError(ExtractionCode.CONTAINMENT)
    # One supervisor invocation owns each observation.
    observation._begin()  # pyright: ignore[reportPrivateUsage] # noqa: SLF001
    if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
        raise ExtractionError(ExtractionCode.INPUT)
    if type(media_type) is not str or media_type not in {"text/plain", "text/html"}:
        raise ExtractionError(ExtractionCode.MEDIA)
    header = (json.dumps({"media_type": media_type, "body_bytes": len(body)}, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    if len(header) > MAX_HEADER_BYTES:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    record, exit_code = await _supervise(body, header, runtime, observation)
    return _result(record, body, media_type, exit_code)


async def extract_rss_feed(
    body: bytes,
    *,
    media_type: RssMedia,
    runtime: ScoutExtractionRuntime,
    lifecycle: ExtractionLifecycle | None = None,
) -> ExtractedFeed:
    """Extract the exact RSS profile in the shared fixed, network-denied worker.

    This accepts no acquisition locator, grants, provider options or parser code.
    Cancellation and uncertain cleanup have the same owned lifecycle as text
    extraction; the structured response grants neither contact nor custody.
    """
    observation = ExtractionLifecycle() if lifecycle is None else lifecycle
    if type(observation) is not ExtractionLifecycle:
        raise ExtractionError(ExtractionCode.CONTAINMENT)
    observation._begin()  # pyright: ignore[reportPrivateUsage] # noqa: SLF001
    if type(body) is not bytes or len(body) > RSS_MAX_BODY_BYTES:
        raise ExtractionError(ExtractionCode.INPUT)
    if type(media_type) is not str or media_type not in RSS_MEDIA:
        raise ExtractionError(ExtractionCode.MEDIA)
    header = (
        json.dumps({"profile": RSS_PROFILE, "media_type": media_type, "body_bytes": len(body)}, separators=(",", ":"))
        + "\n"
    ).encode("ascii")
    if len(header) > MAX_HEADER_BYTES:
        raise ExtractionError(ExtractionCode.PROTOCOL)
    record, exit_code = await _supervise(body, header, runtime, observation)
    return _feed_result(record, body, media_type, exit_code)


async def _supervise(
    body: bytes, header: bytes, runtime: ScoutExtractionRuntime, observation: ExtractionLifecycle
) -> tuple[bytes, int]:
    """Own the same bounded launch, pipes and complete cleanup for both profiles."""
    command = _command(runtime)
    observation._child = None  # pyright: ignore[reportPrivateUsage] # noqa: SLF001 - Supervisor owns the lifecycle.
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
            cwd="/",
            close_fds=True,
            start_new_session=True,
            limit=_PIPE_CHUNK,
        )
    )
    try:
        async with asyncio.timeout(_WALL_SECONDS):
            process = await asyncio.shield(spawn)
            record, exit_code = await _exchange(process, header, body)
    except TimeoutError:
        raise ExtractionError(ExtractionCode.TIMEOUT) from None
    except OSError:
        raise ExtractionError(ExtractionCode.CONTAINMENT) from None
    finally:
        await _finish_cleanup(spawn, observation)
    return record, exit_code
