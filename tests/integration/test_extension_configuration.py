"""Phoenix selection across real interpreter generations, without host operation.

TOML is a deliberate fixture: this does not execute ConfigWriter's Linux
journaled inscription, bind units, start Phoenix, or prove database recovery.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from lychd.config.runes.markers import SAMPLE_MARKER
from lychd.config.runes.registry import RuneRegistry, load_rune_registry
from lychd.config.settings import Settings, get_settings
from lychd.domain.animation.services.adapters.registry import RuntimeAdapterRegistry
from lychd.domain.animation.transmute import Transmuter
from lychd.domain.codex.runes import CodexPreauthRune
from lychd.extensions.host import AssembledExtensions, get_extensions
from lychd.system.constants import PATH_LYCHD_TOML, PATH_RUNES_DIR
from lychd.system.schemas import QuadletBase, QuadletContainer, QuadletPod

_PHOENIX_MODULE = "lychd.extensions.builtin.observability.phoenix"
_SETTINGS = '[server.database]\nphoenix_user = "eye_reader"\nphoenix_password_secret = "eye_password"\n'
_SELECTED = _SETTINGS + '[extensions]\nbuiltins = ["observability/phoenix"]\ncrypt = []\n'
_RUNE = """name = "desk-eye"
host = "localhost"
ui_port = 16006
otlp_port = 14317

[quadlet]
image = "localhost/phoenix-fixture:1"
"""


def _project(settings: Settings, extensions: AssembledExtensions, runes: RuneRegistry) -> list[QuadletBase]:
    return Transmuter(
        settings=settings,
        runtime_planner=RuntimeAdapterRegistry(adapters=extensions.runtime_adapters),
        contributors=extensions.quadlet_contributors,
    ).transmute_all((), runes=runes)


def _assert_no_phoenix(manifests: list[QuadletBase]) -> None:
    assert {manifest.container_name for manifest in manifests if isinstance(manifest, QuadletContainer)} == {
        "lychd-vessel",
        "lychd-phylactery",
        "lychd-migrate",
    }
    pod = next(manifest for manifest in manifests if isinstance(manifest, QuadletPod))
    assert len(pod.publish_ports) == 2  # Only the core listener and database ports.


def _assert_disabled_generation() -> None:
    settings = get_settings()
    extensions = get_extensions()
    assert settings.extensions.builtins == ()
    assert settings.extensions.crypt == ()
    assert extensions.rune_schemas == (CodexPreauthRune,)
    assert extensions.quadlet_contributors == ()
    assert extensions.context.transmutation.registrations == ()
    runes = load_rune_registry(extensions)
    assert runes.all() == ()
    _assert_no_phoenix(_project(settings, extensions, runes))
    assert not any(name.startswith(_PHOENIX_MODULE) for name in sys.modules)


def _selected_generation() -> tuple[Settings, AssembledExtensions, RuneRegistry]:
    settings = get_settings()
    assert settings.extensions.builtins == ("observability/phoenix",)
    extensions = get_extensions()
    assert f"{_PHOENIX_MODULE}.register" in sys.modules

    # Import the expected identities only after the real host selected the shim.
    from lychd.extensions.builtin.observability.phoenix.config import ObservabilityConfig, PhoenixSettings
    from lychd.extensions.builtin.observability.phoenix.contributor import PhoenixQuadletContributor

    assert extensions.rune_schemas == (CodexPreauthRune, ObservabilityConfig, PhoenixSettings)
    (registration,) = extensions.context.transmutation.registrations
    assert registration.registrant_id == "builtin:observability/phoenix"
    assert type(registration.contributor) is PhoenixQuadletContributor
    assert extensions.quadlet_contributors == (registration.contributor,)
    assert extensions.runtime_adapters == ()
    assert extensions.portal_definitions == ()
    return settings, extensions, load_rune_registry(extensions)


def _assert_active_generation_survives_settings_edit() -> None:
    settings, extensions, runes = _selected_generation()

    from lychd.extensions.builtin.observability.phoenix.config import PhoenixSettings

    (rune,) = runes.all()
    assert type(rune) is PhoenixSettings
    assert rune.source_file == PATH_RUNES_DIR / "observability" / "phoenix" / "desk.toml"
    assert rune.name == "desk-eye"
    with pytest.raises(ValidationError, match="frozen_instance"):
        rune.ui_port = 6006

    manifests = _project(settings, extensions, runes)
    phoenix = next(
        manifest
        for manifest in manifests
        if isinstance(manifest, QuadletContainer) and manifest.container_name == "lychd-desk-eye"
    )
    assert phoenix.image == "localhost/phoenix-fixture:1"
    assert phoenix.pod == "lychd.pod"
    assert phoenix.env_vars["PHOENIX_POSTGRES_USER"] == "eye_reader"
    assert phoenix.secrets == ["eye_password,type=env,target=PHOENIX_POSTGRES_PASSWORD"]
    assert phoenix.requires == ["lychd-migrate.service"]
    pod = next(manifest for manifest in manifests if isinstance(manifest, QuadletPod))
    assert pod.publish_ports[-2:] == ["127.0.0.1:16006:6006", "127.0.0.1:14317:4317"]
    assert len(pod.publish_ports) == 4

    PATH_LYCHD_TOML.write_text(_SETTINGS, encoding="utf-8")
    assert get_settings() is settings
    assert get_settings().extensions.builtins == ("observability/phoenix",)
    assert get_extensions() is extensions
    assert _project(get_settings(), get_extensions(), runes) == manifests


def _run_generation(root: Path, phase: str) -> None:
    # A fresh interpreter and explicit environment prevent pytest imports,
    # operator configuration, and cache-clearing from deciding the outcome.
    result = subprocess.run(  # noqa: S603 - fixed interpreter and this test module; no shell
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), phase],
        env={
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "LYCHD_DB_PASSWORD": "fixture-database",
            "LYCHD_RUNTIME_DB_PASSWORD": "fixture-runtime",
            "LYCHD_PHOENIX_DB_PASSWORD": "fixture-phoenix",
            "LYCHD_APP_SECRET_KEY": "fixture-signing",
            "LYCHD_LOCAL_ACCESS_PASSWORD": "fixture-access-password-at-least-32-characters",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, f"{phase}:\n{result.stdout}\n{result.stderr}"


def test_phoenix_selection_and_runes_belong_to_one_process_generation(tmp_path: Path) -> None:
    codex = tmp_path / "config" / "lychd"
    anchor = codex / "runes" / "observability" / "phoenix"
    anchor.mkdir(parents=True)
    settings_file = codex / "lychd.toml"
    settings_file.write_text(_SETTINGS, encoding="utf-8")
    sample_file = anchor / "sample.toml"
    sample_bytes = f"{SAMPLE_MARKER}\n{_RUNE}".encode()
    sample_file.write_bytes(sample_bytes)

    crypt = tmp_path / "data" / "lychd"
    adjacent_shim = crypt / "extensions" / "unselected" / "register.py"
    adjacent_shim.parent.mkdir(parents=True)
    adjacent_shim.write_text(
        'from pathlib import Path\nPath(__file__).with_name("imported").touch()\n'
        'raise RuntimeError("an unselected Crypt package was imported")\n',
        encoding="utf-8",
    )
    retained = crypt / "postgres" / "data" / "retained.bin"
    retained.parent.mkdir(parents=True)
    retained_bytes = b"representative retained bytes\x00\xff\n"
    retained.write_bytes(retained_bytes)

    _run_generation(tmp_path, "default-disabled")
    settings_file.write_text(_SELECTED, encoding="utf-8")
    _run_generation(tmp_path, "selected-inactive")

    rune_file = anchor / "desk.toml"
    rune_bytes = _RUNE.encode()
    rune_file.write_bytes(rune_bytes)
    _run_generation(tmp_path, "active-then-settings-edit")
    assert settings_file.read_text(encoding="utf-8") == _SETTINGS
    _run_generation(tmp_path, "fresh-disabled")

    assert rune_file.read_bytes() == rune_bytes
    assert sample_file.read_bytes() == sample_bytes
    assert retained.read_bytes() == retained_bytes
    assert not adjacent_shim.with_name("imported").exists()
    assert not (tmp_path / "config" / "containers").exists()
    assert not (tmp_path / "config" / "systemd").exists()


if __name__ == "__main__":
    assert not any(name.startswith(_PHOENIX_MODULE) for name in sys.modules)
    match sys.argv[1]:
        case "default-disabled" | "fresh-disabled":
            _assert_disabled_generation()
        case "selected-inactive":
            selected_settings, selected_extensions, selected_runes = _selected_generation()
            assert selected_runes.all() == ()
            _assert_no_phoenix(_project(selected_settings, selected_extensions, selected_runes))
        case "active-then-settings-edit":
            _assert_active_generation_survives_settings_edit()
        case _:
            pytest.fail(f"Unknown generation phase: {sys.argv[1]}")
    assert not any(name.startswith("lychd_crypt_extension_") for name in sys.modules)
