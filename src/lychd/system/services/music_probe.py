"""Finite offline engineering renders, isolated from the Vessel's imports and state.

Only the owned FAUST programs below can execute. Inputs are immutable PCM bytes;
completed cache entries publish a verified receipt last. This worker does not own
musical acceptance, projects, Invocations, live audio, or a native effect capability.
"""

from __future__ import annotations

import importlib
import io
import json
import math
import os
import platform
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import wave
from contextlib import suppress
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, Protocol, cast

from lychd.compositions.riffmaw.offline import models as recipe_models
from lychd.compositions.riffmaw.offline.models import (
    MAX_SOURCE_BYTES,
    AudioTrack,
    RenderedFile,
    RenderReceipt,
    RenderRecipe,
    SynthTrack,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    type Audio = NDArray[np.float32]

BLOCK_SIZE = 64
MAX_TIMEOUT_SECONDS = 30.0
_PROCESS_SETTLEMENT_SECONDS = 1.0
_PROCESS_SETTLEMENT_POLL_SECONDS = 0.001
_PROC_ROOT = Path("/proc")
MAX_LOG_BYTES = 65536
MAX_REQUEST_BYTES = 2 * 1024 * 1024
PCM_WIDTH = 2
PCM24_WIDTH = 3
STEREO_CHANNELS = 2
WORKER_ARGUMENT_COUNT = 3
NEAR_FULL_SCALE = 0.98
_HEADER = 'import("stdfaust.lib");\nfreq=hslider("freq",440,20,20000,1);\ngain=hslider("gain",0.5,0,1,0.01);\ngate=button("gate");\n'
_INSTRUMENTS = {
    "sine": _HEADER + "process=os.osc(freq)*gain*gate <: _,_;",
    "saw": _HEADER + "process=os.sawtooth(freq)*gain*gate <: _,_;",
    "kick": _HEADER + "env=en.adsr(0.001,0.18,0.0,0.02,gate); process=os.osc(freq*(1+3*env))*gain*env <: _,_;",
}
_CONTROLS = """import("stdfaust.lib");
level=hslider("level",1,0,2,0.001);
left=hslider("left",0.707,0,1,0.001);
right=hslider("right",0.707,0,1,0.001);
cutoff=hslider("cutoff",6000,40,20000,1);
process=_,_ : fi.lowpass(1,cutoff),fi.lowpass(1,cutoff) : *(level*left),*(level*right);
"""
_AUDIO_CONTROLS = """level=hslider("level",1,0,2,0.001);
left=hslider("left",0.707,0,1,0.001);
right=hslider("right",0.707,0,1,0.001);
process=_,_ : *(level*left),*(level*right);
"""
_WINDOW = "process(a,b,c,d)=a*c,b*d;"
PROCESSOR_DIGEST = sha256(
    json.dumps([_INSTRUMENTS, _CONTROLS, _AUDIO_CONTROLS, _WINDOW], sort_keys=True).encode()
).hexdigest()


class _Processor(Protocol):
    record: bool

    def get_audio(self) -> Audio: ...


class _Faust(_Processor, Protocol):
    num_voices: int

    def set_dsp_string(self, code: str) -> bool: ...

    def compile(self) -> bool: ...

    def set_parameter(self, path: str, value: float) -> bool: ...

    def add_midi_note(self, note: int, velocity: int, start: float, duration: float) -> bool: ...


class _Engine(Protocol):
    def make_faust_processor(self, name: str) -> _Faust: ...

    def make_playback_processor(self, name: str, data: Audio) -> _Processor: ...

    def make_add_processor(self, name: str, gains: list[float]) -> _Processor: ...

    def load_graph(self, graph: list[tuple[_Processor, list[str]]]) -> bool: ...

    def set_bpm(self, bpm: float) -> None: ...

    def render(self, duration: float) -> bool: ...


class _Native(Protocol):
    def RenderEngine(self, sample_rate: int, block_size: int) -> _Engine: ...  # noqa: N802


class MusicProbeError(RuntimeError):
    """Refused input, unavailable processor, or incomplete technical output."""


def _refuse(message: str) -> NoReturn:
    raise MusicProbeError(message)


def _external_directory(path: Path, *, create: bool = False) -> Path:
    """Require literal absolute directories outside LychD checkout ancestors."""
    if not path.is_absolute() or ".." in path.parts:
        _refuse("Probe workspaces must be absolute paths without parent traversal.")
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            _refuse("Symlink workspace paths are refused.")
        if (ancestor / ".git").exists() and (ancestor / "src" / "lychd").is_dir():
            _refuse("Media workspaces must remain outside repository checkouts.")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        _refuse("A probe workspace directory is missing.")
    return path


def _read_file(path: Path, limit: int) -> bytes:
    """Read a bounded regular file with no symlinks in any path component."""
    try:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parent.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                _refuse("A probe file exceeds its bound or is not a regular file.")
            result = stream.read(limit + 1)
            if len(result) > limit or len(result) != info.st_size:
                _refuse("A probe file changed during reading or exceeds its bound.")
            return result
    except OSError as error:
        msg = "A probe file is missing, inaccessible, or a symlink."
        raise MusicProbeError(msg) from error


def _pcm_info(data: bytes) -> tuple[int, int, int, int, bytes]:
    """Inspect the complete data chunk, including any incomplete final frame."""
    try:
        with wave.open(io.BytesIO(data), "rb") as stream:
            rate, channels, width, frames = (
                stream.getframerate(),
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getnframes(),
            )
            if stream.getcomptype() != "NONE" or channels not in (1, 2) or width not in (2, 3, 4):
                _refuse("Only mono/stereo PCM WAV with 16, 24, or 32 integer bits is admitted.")
            payload = stream.readframes(frames + 1)
            if len(payload) != frames * channels * width:
                _refuse("A PCM source is truncated or contains an incomplete frame.")
            return rate, channels, width, frames, payload
    except (wave.Error, EOFError) as error:
        msg = "Malformed or unsupported PCM WAV."
        raise MusicProbeError(msg) from error


def _sources(recipe: RenderRecipe, assets: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for source in recipe.sources:
        data = _read_file(assets / f"{source.digest}.wav", MAX_SOURCE_BYTES)
        if len(data) != source.size_bytes or sha256(data).hexdigest() != source.digest:
            _refuse("Source size or digest differs from its declaration.")
        rate, _, _, frames, _ = _pcm_info(data)
        if rate != recipe.sample_rate:
            _refuse("Source sample rate differs; implicit resampling is refused.")
        for track in recipe.tracks:
            if isinstance(track, AudioTrack):
                for clip in track.clips:
                    if clip.source_id == source.source_id and clip.source_offset_frames + clip.duration_frames > frames:
                        _refuse("A clip extends beyond its source window.")
        result[source.digest] = data
    return result


def _package_fingerprint(name: str) -> list[tuple[str, str]]:
    installed = distribution(name)
    if installed.files is None:
        _refuse(f"{name} distribution has no artifact inventory.")
    files = sorted(
        file
        for file in installed.files or ()
        if ".so" in str(file)
        or str(file).endswith((".py", ".dll", ".dylib"))
        or ".libs/" in str(file)
        or "/faustlibraries/" in str(file)
    )
    if not files:
        _refuse(f"{name} distribution has no executable artifacts.")
    closure: list[tuple[str, str]] = []
    for file in files:
        digest = sha256()
        with Path(str(installed.locate_file(file))).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        closure.append((str(file), digest.hexdigest()))
    return closure


def _runtime_fingerprint() -> tuple[str, str]:
    try:
        engine_version = version("dawdreamer")
        closure = [_package_fingerprint("dawdreamer"), _package_fingerprint("numpy")]
    except PackageNotFoundError as error:
        msg = "The optional music-probe dependency group is required."
        raise MusicProbeError(msg) from error
    digest = sha256(
        json.dumps(
            [
                closure,
                version("numpy"),
                engine_version,
                sys.version,
                platform.machine(),
                platform.system(),
                platform.libc_ver(),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return engine_version, digest


def _implementation_digest() -> str:
    return sha256(Path(__file__).read_bytes() + Path(recipe_models.__file__).read_bytes()).hexdigest()


def _identity(recipe: RenderRecipe) -> tuple[str, str, str]:
    engine_version, runtime_digest = _runtime_fingerprint()
    implementation = _implementation_digest()
    key = sha256(
        json.dumps([recipe.content_digest(), implementation, PROCESSOR_DIGEST, runtime_digest, BLOCK_SIZE]).encode()
    ).hexdigest()
    return key, f"music-probe-v1-{implementation[:24]}", engine_version


def _observe(path: Path, recipe: RenderRecipe) -> RenderedFile:
    data = _read_file(path, MAX_SOURCE_BYTES)
    rate, channels, width, frames, payload = _pcm_info(data)
    if (rate, channels, width, frames) != (recipe.sample_rate, 2, PCM_WIDTH, recipe.duration_frames):
        _refuse("An output does not match the declared stereo PCM format or frame window.")
    peak_integer = max((abs(value[0]) for value in struct.iter_unpack("<h", payload)), default=0)
    return RenderedFile(
        name=path.name,
        digest=sha256(data).hexdigest(),
        size_bytes=len(data),
        frames=frames,
        sample_rate=cast("LiteralRate", rate),
        peak=peak_integer / 32768.0,
    )


if TYPE_CHECKING:
    from typing import Literal

    type LiteralRate = Literal[32000, 44100, 48000]


def _verify_cache(directory: Path, recipe: RenderRecipe, key: str, revision: str, engine_version: str) -> RenderReceipt:
    if directory.is_symlink() or not directory.is_dir():
        _refuse("A cache entry is not a literal directory.")
    try:
        receipt = RenderReceipt.model_validate_json(_read_file(directory / "receipt.json", MAX_REQUEST_BYTES))
    except ValueError as error:
        msg = "A cache receipt is malformed."
        raise MusicProbeError(msg) from error
    _, engine_digest = _runtime_fingerprint()
    implementation_digest = _implementation_digest()
    if (
        receipt.cache_key,
        receipt.recipe_digest,
        receipt.renderer_revision,
        receipt.engine_version,
        receipt.processor_digest,
        receipt.implementation_digest,
        receipt.engine_digest,
        receipt.sample_rate,
        receipt.duration_frames,
        receipt.block_size,
    ) != (
        key,
        recipe.content_digest(),
        revision,
        engine_version,
        PROCESSOR_DIGEST,
        implementation_digest,
        engine_digest,
        recipe.sample_rate,
        recipe.duration_frames,
        BLOCK_SIZE,
    ):
        _refuse("A cache receipt does not match this recipe and implementation.")
    expected = {"mix.wav", *(f"stem_{track.track_id}.wav" for track in recipe.tracks)}
    if {output.name for output in receipt.outputs} != expected or len(receipt.outputs) != len(expected):
        _refuse("A cache receipt has missing, duplicate, or unexpected outputs.")
    if {path.name for path in directory.iterdir()} != {*expected, "receipt.json"}:
        _refuse("A cache entry has incomplete or unexpected files.")
    for output in receipt.outputs:
        if _observe(directory / output.name, recipe) != output:
            _refuse("Cached audio differs from its digest, format, or peak receipt.")
    return receipt


def _drain_process(process: subprocess.Popen[bytes], deadline: float) -> bytearray:
    """Drain both owned pipes with a bounded byte and time budget."""
    logs = bytearray()
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _refuse("Native render exceeded its process time bound.")
            for event, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(event.fd, 4096)
                if not chunk:
                    selector.unregister(event.fileobj)
                elif len(logs) + len(chunk) > MAX_LOG_BYTES:
                    _refuse("Native diagnostics exceeded their byte bound.")
                else:
                    logs.extend(chunk)
    return logs


def _group_has_live_processes(group_id: int, deadline: float) -> bool:
    """Observe the owned Linux group, including a dead leader's live threads."""
    try:
        for entry in _PROC_ROOT.iterdir():
            if time.monotonic() >= deadline:
                _refuse("Native process-group settlement exceeded its cleanup bound.")
            if not entry.name.isdecimal():
                continue
            try:
                if os.getpgid(int(entry.name)) != group_id:
                    continue
                # comm may contain spaces and ')'; kernel fields follow the last ')'.
                fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
                observed_group = int(fields[2])
                thread_count = int(fields[17])
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (IndexError, ValueError) as error:
                msg = "Native process-group state could not be decoded."
                raise MusicProbeError(msg) from error
            # pthread_exit can leave a zombie leader while other threads still run.
            if observed_group == group_id and (fields[0] not in {b"Z", b"X", b"x"} or thread_count > 1):
                return True
    except OSError as error:
        msg = "Native process-group settlement could not be observed."
        raise MusicProbeError(msg) from error
    return False


def _settle_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill and observe this owned group within a separate finite cleanup budget.

    Waiting for its direct leader alone does not settle descendants. This observes
    the original group only; it does not reap unrelated children or cover processes
    that escaped into another session.
    """
    deadline = time.monotonic() + _PROCESS_SETTLEMENT_SECONDS
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        msg = "Native process-group settlement exceeded its cleanup bound."
        raise MusicProbeError(msg) from error
    while _group_has_live_processes(process.pid, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _refuse("Native process-group settlement exceeded its cleanup bound.")
        time.sleep(min(remaining, _PROCESS_SETTLEMENT_POLL_SECONDS))


def _run_process(command: list[str], timeout_seconds: float, directory: Path) -> None:
    """Drain bounded native diagnostics and observe settlement of the owned group."""
    deadline = time.monotonic() + timeout_seconds
    # Internal callers supply sys.executable and an owned worker module only.
    process = subprocess.Popen(  # noqa: S603 - Only the owned executable/module enter production calls.
        command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    try:
        logs = _drain_process(process, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _refuse("Native render exceeded its process time bound.")
        try:
            result = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            msg = "Native render exceeded its process time bound."
            raise MusicProbeError(msg) from error
        if result:
            msg = f"Native render failed ({result}): {logs[-4096:].decode(errors='replace')}"
            raise MusicProbeError(msg)
    finally:
        try:
            _settle_process_group(process)
        finally:
            # Popen.__exit__ waits without a bound, so close pipes explicitly.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _sync_path(path: Path, *, directory: bool = False) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def render_probe(
    recipe: RenderRecipe, assets_dir: Path, output_dir: Path, *, timeout_seconds: float = 30.0
) -> RenderReceipt:
    """Render or verify one immutable external cache entry; never repair/overwrite it.

    Outputs live under ``output_dir / receipt.cache_key``. Integer PCM cannot
    preserve overload: peaks above full scale refuse instead of silently clipping.
    The timeout covers the native subprocess; validation is bounded by schema sizes.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        _refuse("Native timeout must be positive and at most 30 seconds.")
    # Frozen models can still be constructed without validation by Python callers.
    recipe = RenderRecipe.model_validate_json(recipe.model_dump_json())
    assets = _external_directory(assets_dir)
    outputs = _external_directory(output_dir, create=True)
    sources = _sources(recipe, assets)
    key, revision, engine_version = _identity(recipe)
    destination = outputs / key
    if destination.exists() or destination.is_symlink():
        return _verify_cache(destination, recipe, key, revision, engine_version)
    with tempfile.TemporaryDirectory(prefix=".music-probe-", dir=outputs) as temporary:
        stage = Path(temporary)
        source_dir = stage / "sources"
        source_dir.mkdir(mode=0o700)
        for digest, data in sources.items():
            (source_dir / f"{digest}.wav").write_bytes(data)
        request = stage / "request.json"
        request.write_text(recipe.model_dump_json())
        _run_process([sys.executable, str(Path(__file__).resolve()), "--worker", str(request)], timeout_seconds, stage)
        receipt = _verify_cache(stage / "result", recipe, key, revision, engine_version)
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError:
            return _verify_cache(destination, recipe, key, revision, engine_version)
        # Exclusive hard links publish each immutable file atomically; receipt last
        # is the completion marker. Interrupted publication remains a refused entry.
        for output in receipt.outputs:
            _sync_path(stage / "result" / output.name)
            os.link(stage / "result" / output.name, destination / output.name, follow_symlinks=False)
        _sync_path(destination, directory=True)
        _sync_path(stage / "result" / "receipt.json")
        os.link(stage / "result" / "receipt.json", destination / "receipt.json", follow_symlinks=False)
        _sync_path(destination, directory=True)
        _sync_path(outputs, directory=True)
        return _verify_cache(destination, recipe, key, revision, engine_version)


def _decode(data: bytes) -> Audio:
    import numpy as np

    _, channels, width, _, payload = _pcm_info(data)
    if width == PCM24_WIDTH:
        raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        integers = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        integers = (integers ^ 0x800000) - 0x800000
    else:
        integers = np.frombuffer(payload, dtype="<i2" if width == PCM_WIDTH else "<i4")
    audio = (integers.astype(np.float32) / float(1 << (width * 8 - 1))).reshape(-1, channels).T
    return np.repeat(audio, 2, axis=0) if channels == 1 else audio


def _compile(processor: _Faust, source: str) -> None:
    if not processor.set_dsp_string(source) or not processor.compile():
        _refuse("An owned FAUST processor could not compile.")


def _native_render(recipe: RenderRecipe, stage: Path) -> None:  # noqa: C901, PLR0912, PLR0915
    """Execute only inside the finite child process, with lazy optional imports."""
    import numpy as np

    native = cast("_Native", importlib.import_module("dawdreamer"))
    engine = native.RenderEngine(recipe.sample_rate, BLOCK_SIZE)
    engine.set_bpm(recipe.tempo_bpm)
    graph: list[tuple[_Processor, list[str]]] = []
    stems: list[tuple[str, _Processor]] = []
    snapshots = _sources(recipe, stage / "sources")
    decoded = {source.source_id: _decode(snapshots[source.digest]) for source in recipe.sources}
    for track in recipe.tracks:
        source_name = f"input_{track.track_id}"
        if isinstance(track, SynthTrack):
            instrument = engine.make_faust_processor(source_name)
            # Enough voices for every simultaneous admitted note; fixed allocation
            # avoids DawDreamer's default voice stealing changing the recipe.
            starts = sorted((note.start_frame, 1) for note in track.notes)
            ends = sorted((note.start_frame + note.duration_frames, -1) for note in track.notes)
            active = peak_voices = 0
            for _, delta in sorted(starts + ends):
                active += delta
                peak_voices = max(peak_voices, active)
            instrument.num_voices = peak_voices
            _compile(instrument, _INSTRUMENTS[track.instrument])
            for note in track.notes:
                if not instrument.add_midi_note(
                    note.pitch,
                    note.velocity,
                    note.start_frame / recipe.sample_rate,
                    note.duration_frames / recipe.sample_rate,
                ):
                    _refuse("An admitted MIDI note was refused by the engine.")
            source: _Processor = instrument
        else:
            audio = np.zeros((2, recipe.duration_frames), dtype=np.float32)
            for clip in track.clips:
                audio[:, clip.start_frame : clip.start_frame + clip.duration_frames] += decoded[clip.source_id][
                    :, clip.source_offset_frames : clip.source_offset_frames + clip.duration_frames
                ]
            source = engine.make_playback_processor(source_name, audio)
        graph.append((source, []))
        control_name = f"control_{track.track_id}"
        control = engine.make_faust_processor(control_name)
        _compile(control, _CONTROLS if isinstance(track, SynthTrack) else _AUDIO_CONTROLS)
        angle = (track.pan + 1.0) * math.pi / 4.0
        cutoff = (
            min(track.cutoff_hz, recipe.sample_rate * 0.49)
            if isinstance(track, SynthTrack)
            else recipe.sample_rate * 0.49
        )
        controls = [("level", 10 ** (track.gain_db / 20.0)), ("left", math.cos(angle)), ("right", math.sin(angle))]
        if isinstance(track, SynthTrack):
            controls.append(("cutoff", cutoff))
        for parameter, value in controls:
            if not control.set_parameter(f"/dawdreamer/{parameter}", value):
                _refuse("An admitted control was refused by the owned processor.")
        graph.append((control, [source_name]))
        mask = np.zeros((STEREO_CHANNELS, recipe.duration_frames), dtype=np.float32)
        for event in track.notes if isinstance(track, SynthTrack) else track.clips:
            mask[:, event.start_frame : event.start_frame + event.duration_frames] = 1.0
        mask_name = f"window_{track.track_id}"
        graph.append((engine.make_playback_processor(mask_name, mask), []))
        stem_name = f"stem_{track.track_id}"
        fence = engine.make_faust_processor(stem_name)
        _compile(fence, _WINDOW)
        fence.record = True
        graph.append((fence, [control_name, mask_name]))
        stems.append((stem_name, fence))
    mixer = engine.make_add_processor("mix", [1.0] * len(stems))
    mixer.record = True
    graph.append((mixer, [name for name, _ in stems]))
    if not engine.load_graph(graph) or not engine.render((recipe.duration_frames + BLOCK_SIZE) / recipe.sample_rate):
        _refuse("The admitted deterministic processor graph did not render.")
    result = stage / "result"
    result.mkdir(mode=0o700)
    observations: list[RenderedFile] = []
    warnings: list[str] = []
    # Frame fences and the mixer are graph nodes in the same native render.
    for name, processor in [*stems, ("mix", mixer)]:
        audio = np.array(processor.get_audio()[:, : recipe.duration_frames], dtype=np.float32, copy=True)
        if audio.shape != (2, recipe.duration_frames) or not np.isfinite(audio).all():
            _refuse("Native output has wrong frame alignment or non-finite samples.")
        observations.append(_write_audio(result / f"{name}.wav", audio, recipe, warnings))
    key, revision, engine_version = _identity(recipe)
    receipt = RenderReceipt(
        recipe_digest=recipe.content_digest(),
        cache_key=key,
        renderer_revision=revision,
        implementation_digest=_implementation_digest(),
        engine_digest=_runtime_fingerprint()[1],
        engine_version=engine_version,
        processor_digest=PROCESSOR_DIGEST,
        sample_rate=recipe.sample_rate,
        block_size=BLOCK_SIZE,
        duration_frames=recipe.duration_frames,
        outputs=tuple(observations),
        warnings=tuple(warnings),
    )
    (result / "receipt.json").write_text(receipt.model_dump_json())


def _write_audio(path: Path, audio: Audio, recipe: RenderRecipe, warnings: list[str]) -> RenderedFile:
    import numpy as np

    peak = float(np.max(np.abs(audio)))
    if not math.isfinite(peak) or peak > 1.0:
        _refuse("Audio exceeds integer PCM full scale; lower gains before rendering.")
    if peak >= NEAR_FULL_SCALE:
        warnings.append(f"{path.name} approaches integer PCM full scale.")
    # Scaling by 32767 admits +1 without overflowing a signed 16-bit sample.
    pcm = np.rint(audio.T * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(PCM_WIDTH)
        stream.setframerate(recipe.sample_rate)
        stream.writeframes(pcm.tobytes())
    return _observe(path, recipe)


if __name__ == "__main__":
    if len(sys.argv) != WORKER_ARGUMENT_COUNT or sys.argv[1] != "--worker":
        _refuse("This module only admits its finite internal worker invocation.")
    request_path = Path(sys.argv[2])
    _native_render(RenderRecipe.model_validate_json(_read_file(request_path, MAX_REQUEST_BYTES)), request_path.parent)
