"""Explicit finite Linux microphone capture for the engineering speech probe."""

from __future__ import annotations

import asyncio
import os
import signal
import struct
from contextlib import suppress
from typing import Literal

from lychd.lib.asyncio import complete_under_cancellation

SAMPLE_RATE = 16_000
MAX_CAPTURE_SECONDS = 15


class VoiceCaptureError(RuntimeError):
    """Capture failed after the owned recorder group retired; a new gesture may retry."""


class VoiceCaptureUnsettled(VoiceCaptureError):  # noqa: N818 - caller contract names unsettled ownership
    """Recorder retirement is unproven; further audio must remain closed."""


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _join_group(process: asyncio.subprocess.Process) -> None:
    await process.wait()
    while _group_exists(process.pid):  # noqa: ASYNC110 - process groups have no asyncio exit notification
        await asyncio.sleep(0.02)


async def _retire(process: asyncio.subprocess.Process) -> None:
    # Leader exit alone does not prove that its process group has retired.
    if _group_exists(process.pid):
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        async with asyncio.timeout(2):
            await _join_group(process)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        async with asyncio.timeout(2):
            await _join_group(process)


async def _settle_retirement(process: asyncio.subprocess.Process) -> None:
    closing = asyncio.create_task(_retire(process))
    try:
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            try:
                await complete_under_cancellation(closing)
            except asyncio.CancelledError:
                msg = "Recorder retirement cancelled without settlement; further audio must remain closed."
                raise VoiceCaptureUnsettled(msg) from None
            raise
    except VoiceCaptureUnsettled:
        raise
    except (OSError, RuntimeError, TimeoutError):
        msg = "Recorder retirement unconfirmed; further audio must remain closed."
        raise VoiceCaptureUnsettled(msg) from None


async def capture_utterance(*, seconds: int, device: Literal["default", "pulse"] = "default") -> bytes:
    """Capture only this invocation's exact sample budget; join the owned recorder.

    The caller owns a visible capture indication, consent and private retention.
    No recorder is launched until original argument values pass validation.
    The system ALSA default preserves host routing; explicit Pulse never falls back.
    Only VoiceCaptureError without VoiceCaptureUnsettled proves failed capture retired.
    Unadopted launch errors carry no retirement proof.
    """
    if (
        type(seconds) is not int
        or not 1 <= seconds <= MAX_CAPTURE_SECONDS
        or type(device) is not str
        or device not in {"default", "pulse"}
    ):
        msg = "Capture requires 1-15 whole seconds and an explicit supported local device."
        raise ValueError(msg)
    frames = seconds * SAMPLE_RATE
    expected = frames * 2
    launching = asyncio.create_task(
        asyncio.create_subprocess_exec(
            "arecord",
            "--quiet",
            "--file-type",
            "raw",
            "--format",
            "S16_LE",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            "1",
            "--samples",
            str(frames),
            "--device",
            device,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    )
    try:
        process = await asyncio.shield(launching)
    except asyncio.CancelledError:
        try:
            process = await complete_under_cancellation(launching)
        except asyncio.CancelledError:
            msg = "Recorder launch cancelled without an adopted process; ownership is unconfirmed."
            raise VoiceCaptureUnsettled(msg) from None
        await _settle_retirement(process)
        raise
    try:
        async with asyncio.timeout(seconds + 5):
            if process.stdout is None:
                msg = "Recorder has no bounded output channel."
                raise VoiceCaptureError(msg)
            pcm = bytearray()
            while chunk := await process.stdout.read(min(65_536, expected + 1 - len(pcm))):
                pcm.extend(chunk)
                if len(pcm) > expected:
                    msg = "Recorder exceeded the exact sample budget."
                    raise VoiceCaptureError(msg)
            status = await process.wait()
            if status != 0 or len(pcm) != expected:
                msg = "Recorder failed or returned an incomplete utterance."
                raise VoiceCaptureError(msg)
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            expected + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            SAMPLE_RATE,
            SAMPLE_RATE * 2,
            2,
            16,
            b"data",
            expected,
        )
        return header + bytes(pcm)
    except (OSError, RuntimeError, TimeoutError) as error:
        if isinstance(error, VoiceCaptureError):
            raise
        msg = "Recorder failed before completing the bounded utterance."
        raise VoiceCaptureError(msg) from None
    finally:
        await _settle_retirement(process)
