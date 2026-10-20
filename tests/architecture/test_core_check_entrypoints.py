from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WRAPPER = ROOT / "scripts" / "container_checks.sh"


def test_host_make_test_dispatches_to_oci_without_a_python_fallback() -> None:
    env = {**os.environ, "LYCHD_TEST_CONTAINER": "0"}
    result = subprocess.run(
        ["/usr/bin/make", "--dry-run", "test"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "bash scripts/container_checks.sh test" in result.stdout
    assert "pytest" not in result.stdout


def test_wrapper_bounds_engine_arguments_and_withholds_ambient_credentials(tmp_path: Path) -> None:
    engine = tmp_path / "docker"
    engine.write_text(
        '#!/bin/bash\nprintf "%s\\0" "$@" >> "$ENGINE_LOG"\nprintf "\\n" >> "$ENGINE_LOG"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == --iidfile ]]; then printf "sha256:%064d\\n" 0 > "$2"; break; fi\n'
        '  if [[ "$1" == --cidfile ]]; then printf "%064d\\n" 1 > "$2"; break; fi\n'
        "  shift\ndone\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    log = tmp_path / "calls"
    selection = f'-k "two words"; touch {tmp_path / "should-not-exist"}'
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ENGINE_LOG": str(log),
        "LYCHD_TEST_ENGINE": "docker",
        "LYCHD_TEST_PYTHON": "3.14.8",
        "ARGS": selection,
        "OPENAI_API_KEY": "must-not-cross-the-container-boundary",
    }
    result = subprocess.run(  # noqa: S603 - Fixed wrapper; engine is a local inert test fixture.
        ["/usr/bin/bash", str(WRAPPER), "test"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    calls = [[arg.decode() for arg in line.split(b"\0") if arg] for line in log.read_bytes().splitlines()]
    assert len(calls) == 3
    assert calls[0][0] == "build"
    assert "PYTHON_VERSION=3.14.8" in calls[0]
    run = calls[1]
    assert run[0] == "run"
    assert "--network=none" in run
    assert "--cidfile" in run
    assert run[run.index("--memory") + 1] == "4g"
    assert run[run.index("--cpus") + 1] == "2"
    assert run[run.index("--pids-limit") + 1] == "512"
    assert calls[2] == ["rm", "--force", f"{1:064d}"]
    assert "--mount" not in run
    assert "ARGS" in run
    assert "OPENAI_API_KEY" not in run
    assert run[-2:] == [f"sha256:{'0' * 64}", "test"]
    assert not (tmp_path / "should-not-exist").exists()


def test_wrapper_rejects_an_engine_command_or_arbitrary_make_target() -> None:
    for env, target in (
        ({"LYCHD_TEST_ENGINE": "docker --privileged"}, "test"),
        ({"LYCHD_TEST_ENGINE": "docker"}, "init"),
    ):
        result = subprocess.run(  # noqa: S603 - Fixed wrapper and bounded refusal selectors.
            ["/usr/bin/bash", str(WRAPPER), target],
            cwd=ROOT,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2


def test_core_ci_uses_both_source_bound_oci_interpreters() -> None:
    path = ROOT / ".github" / "workflows" / "core-checks.yml"
    workflow = cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))
    core: dict[str, Any] = workflow["jobs"]["core"]
    assert sorted(core["strategy"]["matrix"]["python-version"]) == ["3.14.8", "3.15.0"]
    assert core["env"]["LYCHD_TEST_PYTHON"] == "${{ matrix.python-version }}"
    steps: list[dict[str, object]] = core["steps"]
    commands: list[list[str]] = []
    for step in steps:
        action: object = step.get("uses")
        if action is not None:
            assert isinstance(action, str)
            assert "setup-uv" not in action
        command: object = step.get("run")
        if command is not None:
            assert isinstance(command, str)
            commands.append(shlex.split(command, comments=True))
    assert ["bash", "scripts/container_checks.sh", "lint", "format-check", "type-check", "test-ci"] in commands
    containerfile = (ROOT / "Containerfile.tests").read_text(encoding="utf-8")
    assert "python:${PYTHON_VERSION}-slim-trixie" in containerfile
    assert "uv:0.13.0" in containerfile
    assert "uv sync --locked" in containerfile


@pytest.mark.parametrize(("phase", "target"), [("build", "test"), ("run", "test"), ("run", "developer-fixture")])
def test_wrapper_only_term_settles_the_tracked_child(tmp_path: Path, phase: str, target: str) -> None:
    engine = tmp_path / "docker"
    engine.write_text(
        '#!/bin/bash\nset -eu\nphase=$1\nprintf "%s\\n" "$phase" >> "$ENGINE_LOG"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == --iidfile ]]; then printf "sha256:%064d\\n" 0 > "$2"; fi\n'
        '  if [[ "$1" == --cidfile ]]; then printf "%064d\\n" 1 > "$2"; fi\n'
        "  shift\ndone\n"
        'if [[ "$phase" == "$HOLD_PHASE" ]]; then\n'
        "  trap 'rm -f -- \"$ENGINE_READY\"; exit 143' TERM INT\n"
        '  touch "$ENGINE_READY"\n'
        "  while :; do sleep 0.1; done\n"
        "fi\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    ready, log = tmp_path / "ready", tmp_path / "calls"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "ENGINE_READY": str(ready),
        "ENGINE_LOG": str(log),
        "HOLD_PHASE": phase,
        "LYCHD_TEST_ENGINE": "docker",
    }
    process = subprocess.Popen(  # noqa: S603 - Fixed wrapper and inert engine fixture; no daemon access.
        ["/usr/bin/bash", str(WRAPPER), target],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "The inert engine did not reach its admitted phase"
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=3)
        assert process.returncode == 143, stderr
        assert not ready.exists(), "The supervised engine survived wrapper-only TERM"
        assert log.read_text().splitlines() == (["build"] if phase == "build" else ["build", "run", "rm"])
        assert not list(tmp_path.glob("lychd-check-context.*"))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
