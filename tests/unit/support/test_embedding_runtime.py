"""Inert admission and containment controls for the optional embedding proof."""

# ruff: noqa: INP001 - focused tests use the existing support namespace

from pathlib import Path

import pytest

from tests.support import embedding_runtime as proof


def test_default_gate_precedes_all_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LYCHD_REQUIRE_EMBEDDING_PROOF", raising=False)
    monkeypatch.setattr(proof, "qualification", lambda: pytest.fail("qualification read before opt-in"))
    with pytest.raises(pytest.skip.Exception):
        proof.run_registered_embedding_proof(tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def test_required_missing_fixture_refuses_before_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LYCHD_REQUIRE_EMBEDDING_PROOF", "1")
    monkeypatch.delenv("LYCHD_EMBEDDING_FIXTURE", raising=False)
    monkeypatch.setattr(proof, "required_tools", lambda: pytest.fail("tool resolution before artifact admission"))
    with pytest.raises(AssertionError, match="LYCHD_EMBEDDING_FIXTURE"):
        proof.run_registered_embedding_proof(tmp_path / "unused")


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "size", "digest"])
def test_bad_artifacts_refuse_without_following_or_reading_unbounded_files(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "public.gguf"
    if kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"abc")
        source.symlink_to(target)
    elif kind != "missing":
        source.write_bytes(b"abc")
    with pytest.raises((AssertionError, FileNotFoundError)):
        proof.verify_artifact(source, size=4 if kind == "size" else 3, digest="0" * 64)


def test_fixed_create_plan_has_no_ambient_network_device_or_fixture_relabel(tmp_path: Path) -> None:
    manifest = proof.qualification()
    args = proof.create_arguments("proof", "token", tmp_path / "public.gguf", manifest)
    assert "--pull=never" in args
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert "--timeout=180" in args
    assert not any(arg.startswith(("--publish", "--device", "--env-file")) for arg in args)
    mount = next(arg for arg in args if arg.startswith("--mount="))
    assert mount.endswith(",destination=/models/embedding.gguf,ro")
    assert "relabel" not in mount
    assert args[args.index("-ngl") + 1] == "0"
    assert manifest["image_id"] in args


def test_image_mismatch_refuses() -> None:
    manifest = proof.qualification()
    with pytest.raises(AssertionError, match="image"):
        proof.require_image(["sha256:" + "0" * 64, [manifest["image_manifest"]]], manifest)
    with pytest.raises(AssertionError, match="manifest"):
        proof.require_image([manifest["image_id"], []], manifest)


def test_engine_environment_replaces_system_defaults_without_mutating_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tomllib

    data = tmp_path / "existing-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("HTTP_PROXY", "must-not-inherit")
    monkeypatch.setenv("CONTAINERS_CONF_OVERRIDE", "must-not-inherit")
    environment = proof._environment(tmp_path / "lab")  # pyright: ignore[reportPrivateUsage] - test config boundary
    assert "HTTP_PROXY" not in environment
    assert "CONTAINERS_CONF_OVERRIDE" not in environment
    assert not data.exists()
    config = tomllib.loads(Path(environment["CONTAINERS_CONF"]).read_text())
    assert config["engine"]["hooks_dir"] == []
    assert config["containers"]["devices"] == []
    assert config["containers"]["volumes"] == []
    assert config["containers"]["env"] == []
    storage = tomllib.loads(Path(environment["CONTAINERS_STORAGE_CONF"]).read_text())
    assert storage["storage"]["graphroot"] == str(data / "containers/storage")
    assert storage["storage"]["options"]["additionalimagestores"] == []


def test_partial_pidfd_tracking_still_stops_and_removes_owned_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lab = proof._Lab(tmp_path / "lab", {})  # pyright: ignore[reportPrivateUsage] - test owned lifetime
    lab.cid = "a" * 64
    lab.start_attempted = True
    commands: list[str] = []

    def podman(*args: str, timeout: float = 25) -> str:
        commands.append(args[0])
        return lab.cid if args[0] == "ps" and "rm" not in commands else ""

    def failed_handles() -> None:
        raise ProcessLookupError

    monkeypatch.setattr(lab, "podman", podman)

    def identity(_selector: str | None = None) -> list[object]:
        return [lab.cid, lab.token, "stop" not in commands]

    monkeypatch.setattr(lab, "identity", identity)
    monkeypatch.setattr(lab, "capture_handles", failed_handles)
    with pytest.raises(AssertionError, match="owned_process_tracking_incomplete"):
        lab.cleanup()
    assert "stop" in commands
    assert "rm" in commands


def test_failed_owned_process_group_and_output_bound(tmp_path: Path) -> None:
    import os
    import select
    import sys

    if sys.platform != "linux":
        pytest.skip("Linux process-group ownership control")
    lab = proof._Lab(tmp_path / "lab", {})  # pyright: ignore[reportPrivateUsage] - test owned lifetime
    pidfile = tmp_path / "child.pid"
    script = """import os, resource, sys, time
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_AS, (128*1024**2, 128*1024**2))
pid = os.fork()
if pid == 0:
    os.close(1)
    os.close(2)
    time.sleep(5)
    os._exit(0)
with open(sys.argv[1], 'w') as stream:
    stream.write(str(pid))
sys.exit(7)
"""
    with pytest.raises(AssertionError, match="command_failed"):
        lab.run([sys.executable, "-I", "-c", script, str(pidfile)], timeout=3)
    try:
        descriptor = os.pidfd_open(int(pidfile.read_text()))
    except ProcessLookupError:
        pass
    else:
        try:
            assert select.select([descriptor], [], [], 2)[0], "failed leader left a live child"
        finally:
            os.close(descriptor)
    with pytest.raises(AssertionError, match="command_output_bound"):
        lab.run([sys.executable, "-I", "-c", "import sys; sys.stdout.write('x'*65537)"], timeout=3)
