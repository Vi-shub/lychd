from __future__ import annotations

import json
from pathlib import Path

import pytest
from click import ClickException
from click.testing import CliRunner
from scripts.music_probe import CHECKOUT_ROOT, cli, external_path, read_json_file

pytest_plugins = ["tests.music_probe_support"]


def test_schema_and_demo_work_without_native_import(probe_path: Path) -> None:
    runner = CliRunner()
    schema = runner.invoke(cli, ["schema"])
    assert schema.exit_code == 0
    assert json.loads(schema.output)["title"] == "RenderRecipe"
    recipe = probe_path / "recipe.json"
    result = runner.invoke(cli, ["demo", "--bars", "1", "--output", str(recipe)])
    assert result.exit_code == 0
    before = recipe.read_bytes()
    second = runner.invoke(cli, ["demo", "--output", str(recipe)])
    assert second.exit_code != 0
    assert recipe.read_bytes() == before
    validated = runner.invoke(cli, ["validate", str(recipe)])
    assert validated.exit_code == 0
    assert json.loads(validated.output)["recipe_digest"] == json.loads(result.output)["recipe_digest"]
    for kind, title in (("comfy-profile", "ComfyProfile"), ("comfy-request", "ComfyRequest")):
        result = runner.invoke(cli, ["schema", kind])
        assert result.exit_code == 0
        assert json.loads(result.output)["title"] == title


def test_render_timeout_refuses_outside_worker_bound_before_files_or_processes(probe_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["render", str(probe_path / "missing.json"), "--workspace", str(probe_path / "work"), "--timeout", "31"]
    )
    assert result.exit_code == 2
    assert "--timeout" in result.output
    assert not (probe_path / "work").exists()


def test_private_probe_paths_refuse_checkout_relative_and_symlink(probe_path: Path) -> None:
    for path in (CHECKOUT_ROOT / "private.json", Path("relative.json")):
        with pytest.raises(ClickException, match=r"outside|absolute"):
            external_path(path)
    link = probe_path / "link"
    link.symlink_to(probe_path, target_is_directory=True)
    with pytest.raises(ClickException, match="symlink"):
        external_path(link / "private.json")


def test_json_reads_bound_input_bytes(probe_path: Path) -> None:
    oversized = probe_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ClickException, match="byte limit"):
        read_json_file(oversized)
