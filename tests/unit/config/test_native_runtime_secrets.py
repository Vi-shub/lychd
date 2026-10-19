"""Explicit Podman startup credentials use the real Settings source pipeline."""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from pydantic_settings import EnvSettingsSource

from lychd.config.settings import (
    DatabaseSettings,
    ServerSettings,
    Settings,
    SettingsSnapshot,
    WebSettings,
    get_settings,
)
from lychd.config.settings.sources import RuntimeSecretsSource
from lychd.system.services.secrets import PodmanSecretStoreError

_CREDENTIAL_FIELDS = (
    ("database", "password", "LYCHD_DB_PASSWORD"),
    ("database", "runtime_password", "LYCHD_RUNTIME_DB_PASSWORD"),
    ("database", "phoenix_password", "LYCHD_PHOENIX_DB_PASSWORD"),
    ("web", "secret_key", "LYCHD_APP_SECRET_KEY"),
    ("web", "access_password", "LYCHD_LOCAL_ACCESS_PASSWORD"),
)
_RUNTIME_NAMES = ("lychd_runtime_db_password", "lychd_app_secret_key", "lychd_local_access_password")


@dataclass
class _SecretHarness:
    values: dict[str, SecretStr] = field(
        default_factory=lambda: {
            _RUNTIME_NAMES[0]: SecretStr("synthetic-runtime-value\nkept-exact"),
            _RUNTIME_NAMES[1]: SecretStr("synthetic-signing-value\nkept-exact"),
            _RUNTIME_NAMES[2]: SecretStr("synthetic-local-access-" + "x" * 40),
        }
    )
    tools: list[str] = field(default_factory=list)
    adapters: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    trusted_path: str | None = "/usr/bin/podman"
    tool_error: OSError | None = None
    fail_name: str | None = None

    def trusted_tool(self, name: str, *, fallbacks: tuple[Path, ...] | None = None) -> str | None:
        assert fallbacks is None
        self.tools.append(name)
        if self.tool_error is not None:
            raise self.tool_error
        return self.trusted_path

    def adapter(self, podman_bin: str) -> _ReadOnlySecrets:
        self.adapters.append(podman_bin)
        return _ReadOnlySecrets(self)


@dataclass
class _ReadOnlySecrets:
    harness: _SecretHarness

    def read(self, name: str) -> SecretStr:
        self.harness.reads.append(name)
        if name == self.harness.fail_name:
            # A broken adapter diagnostic must never escape into Settings errors.
            detail = " ".join(value.get_secret_value() for value in self.harness.values.values())
            raise PodmanSecretStoreError(detail)
        return self.harness.values[name]


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # The real EnvSettingsSource sees only the test's explicit synthetic inputs.
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr("lychd.config.settings.root.PATH_LYCHD_TOML", tmp_path / "lychd.toml")
    monkeypatch.setattr("lychd.config.settings.sources.MOUNTED_SECRETS_DIR", tmp_path / "mounts")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> _SecretHarness:
    harness = _SecretHarness()
    # Patch existing canonical boundaries, so this file also collects on the old source.
    monkeypatch.setattr("lychd.system.host_tools.trusted_host_tool", harness.trusted_tool)
    monkeypatch.setattr("lychd.system.services.secrets.PodmanSecretStore", harness.adapter)
    return harness


@pytest.fixture
def no_mounted_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_read(path: Path, *args: Any, **kwargs: Any) -> str:
        msg = "Selected runtime source attempted a mounted-file credential read"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "read_text", unexpected_read)


@pytest.fixture
def selected(
    monkeypatch: pytest.MonkeyPatch,
    secrets: _SecretHarness,
    no_mounted_reads: None,
) -> _SecretHarness:
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", "podman-core@1")
    return secrets


def _assert_no_podman(secrets: _SecretHarness) -> None:
    assert secrets.tools == []
    assert secrets.adapters == []
    assert secrets.reads == []


def _assert_runtime_values(settings: Settings, values: dict[str, SecretStr], names: tuple[str, ...]) -> None:
    assert settings.server.database.runtime_password == values[names[0]]
    assert settings.server.web.secret_key == values[names[1]]
    assert settings.server.web.access_password == values[names[2]]
    assert settings.server.database.password is None
    assert settings.server.database.phoenix_password is None


@pytest.mark.usefixtures("no_mounted_reads")
@pytest.mark.parametrize(
    "selection",
    ["", "podman-core", "podman-core@2", "PODMAN-CORE@1", " podman-core@1", "podman-core@1\n", "private-choice"],
)
def test_unknown_selection_refuses_without_tool_or_secret_io(
    monkeypatch: pytest.MonkeyPatch,
    secrets: _SecretHarness,
    selection: str,
) -> None:
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", selection)
    with pytest.raises(ValueError, match="LYCHD_RUNTIME_SECRETS") as error:
        Settings()
    assert "private-choice" not in str(error.value)
    _assert_no_podman(secrets)


def test_absent_selection_preserves_legacy_aliases_and_complete_overrides(
    monkeypatch: pytest.MonkeyPatch,
    secrets: _SecretHarness,
) -> None:
    monkeypatch.setenv("LYCHD_RUNTIME_DB_PASSWORD", "legacy-runtime")
    monkeypatch.setenv("LYCHD_APP_SECRET_KEY", "legacy-signing")
    loaded = Settings()
    assert loaded.server.database.runtime_password == SecretStr("legacy-runtime")
    assert loaded.server.web.secret_key == SecretStr("legacy-signing")
    explicit = Settings(server=ServerSettings())
    assert explicit.server.database.runtime_password is None
    assert explicit.server.web.secret_key is None
    _assert_no_podman(secrets)


def test_selected_source_reads_only_three_validated_declared_names(
    selected: _SecretHarness,
    tmp_path: Path,
) -> None:
    names = ("declared-runtime", "declared-signing", "declared-access")
    selected.values = dict(zip(names, selected.values.values(), strict=True))
    (tmp_path / "lychd.toml").write_text(
        '[server.database]\npassword_secret="never-bootstrap"\n'
        'runtime_password_secret="declared-runtime"\nphoenix_password_secret="never-phoenix"\n'
        '[server.web]\nsecret_key_secret="declared-signing"\naccess_password_secret="declared-access"\n',
        encoding="utf-8",
    )
    loaded = Settings()
    _assert_runtime_values(loaded, selected.values, names)
    assert selected.tools == ["podman"]
    assert selected.adapters == ["/usr/bin/podman"]
    assert selected.reads == list(names)


def test_source_uses_the_already_captured_pydantic_environment(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
) -> None:
    source = RuntimeSecretsSource(Settings, EnvSettingsSource(Settings))
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", "changed-after-capture")
    monkeypatch.setenv("LYCHD_DB_PASSWORD_FILE", "late-competing-file")
    values = source()
    assert values == {
        "server": {
            "database": {"runtime_password": selected.values[_RUNTIME_NAMES[0]]},
            "web": {
                "secret_key": selected.values[_RUNTIME_NAMES[1]],
                "access_password": selected.values[_RUNTIME_NAMES[2]],
            },
        }
    }
    assert selected.tools == ["podman"]
    assert selected.reads == list(_RUNTIME_NAMES)


@pytest.mark.parametrize(("section", "credential", "alias"), _CREDENTIAL_FIELDS)
@pytest.mark.parametrize("value", [None, SecretStr("synthetic-local-access-" + "x" * 40)])
def test_supplied_credential_including_none_refuses_before_reads(
    selected: _SecretHarness,
    section: str,
    credential: str,
    alias: str,
    value: SecretStr | None,
) -> None:
    with pytest.raises(ValueError, match="forbids supplied core credentials"):
        Settings.model_validate({"server": {section: {credential: value}}})
    _assert_no_podman(selected)


@pytest.mark.parametrize(
    "server",
    [ServerSettings(), {"database": DatabaseSettings()}, {"web": WebSettings()}, {}, {"port": 9011}],
)
@pytest.mark.parametrize("partial_update", [False, True])
def test_constructor_server_sections_refuse_before_model_serialization_can_erase_provenance(
    selected: _SecretHarness,
    server: ServerSettings | dict[str, DatabaseSettings | WebSettings | int],
    *,
    partial_update: bool,
) -> None:
    # BaseSettings source options are outside Pydantic's synthesized field signature.
    values: dict[str, Any] = {"server": server, "_nested_model_default_partial_update": partial_update}
    with pytest.raises(ValueError, match="constructor server sections"):
        Settings(**values)
    _assert_no_podman(selected)


@pytest.mark.parametrize(("section", "credential", "alias"), _CREDENTIAL_FIELDS)
@pytest.mark.parametrize("value", ["synthetic-local-access-" + "x" * 40, "null"])
def test_nested_environment_credentials_refuse_including_explicit_none(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
    section: str,
    credential: str,
    alias: str,
    value: str,
) -> None:
    monkeypatch.setenv(f"SERVER__{section.upper()}__{credential.upper()}", value)
    options: dict[str, Any] = {"_env_parse_none_str": "null"}
    with pytest.raises(ValueError, match="forbids supplied core credentials"):
        Settings(**options)
    _assert_no_podman(selected)


@pytest.mark.parametrize(
    ("env_key", "value"),
    [
        ("SERVER", '{"database":{"password":null}}'),
        ("SERVER__DATABASE", '{"phoenix_password":null}'),
        ("SERVER__WEB", '{"secret_key":null}'),
    ],
)
def test_json_environment_sections_cannot_hide_supplied_credentials(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
    env_key: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_key, value)
    with pytest.raises(ValueError, match="forbids supplied core credentials"):
        Settings()
    _assert_no_podman(selected)


@pytest.mark.parametrize(("section", "credential", "alias"), _CREDENTIAL_FIELDS)
@pytest.mark.parametrize("suffix", ["", "_FILE"])
@pytest.mark.parametrize("value", ["", "synthetic-override"])
def test_alias_or_file_override_refuses_even_when_empty(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
    section: str,
    credential: str,
    alias: str,
    suffix: str,
    value: str,
) -> None:
    monkeypatch.setenv(f"{alias}{suffix}", value)
    with pytest.raises(ValueError, match="forbids supplied core credentials"):
        Settings()
    _assert_no_podman(selected)


def test_equal_value_override_is_still_refused(selected: _SecretHarness) -> None:
    with pytest.raises(ValueError, match="forbids supplied core credentials"):
        Settings.model_validate({"server": {"database": {"runtime_password": selected.values[_RUNTIME_NAMES[0]]}}})
    _assert_no_podman(selected)


@pytest.mark.parametrize(("section", "credential", "alias"), _CREDENTIAL_FIELDS)
def test_toml_credentials_refuse_before_podman_reads(
    selected: _SecretHarness,
    tmp_path: Path,
    section: str,
    credential: str,
    alias: str,
) -> None:
    (tmp_path / "lychd.toml").write_text(
        f'[server.{section}]\n{credential}="synthetic-private-value"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="forbids supplied core credentials") as error:
        Settings()
    assert "synthetic-private-value" not in str(error.value)
    _assert_no_podman(selected)


@pytest.mark.parametrize(
    "server",
    [
        {"database": {"runtime_password_secret": "../unsafe"}},
        {"web": {"secret_key_secret": "--unsafe"}},
        {"web": {"access_password_secret": "unsafe,target=/run/other"}},
        {"web": {"secret_key_secret": "lychd_runtime_db_password"}},
    ],
)
def test_invalid_or_aliased_secret_names_refuse_before_tool_resolution(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
    server: dict[str, dict[str, str]],
) -> None:
    monkeypatch.setenv("SERVER", json.dumps(server))
    with pytest.raises(ValueError, match=r"option-free Podman secret name|must use distinct Podman secret names"):
        Settings()
    _assert_no_podman(selected)


def test_unavailable_trusted_tool_has_no_adapter_or_fallback_reads(selected: _SecretHarness) -> None:
    selected.trusted_path = None
    with pytest.raises(ValueError, match="trusted Podman executable"):
        Settings()
    assert selected.tools == ["podman"]
    assert selected.adapters == []
    assert selected.reads == []


def test_tool_resolution_failure_is_sanitized(selected: _SecretHarness) -> None:
    selected.tool_error = OSError("synthetic-private-tool-diagnostic")
    with pytest.raises(ValueError, match="Selected Podman runtime credentials are unavailable") as error:
        Settings()
    assert "synthetic-private-tool-diagnostic" not in "".join(traceback.format_exception(error.value))
    assert selected.tools == ["podman"]
    assert selected.adapters == []
    assert selected.reads == []


@pytest.mark.parametrize("failed_index", [0, 1, 2])
def test_partial_read_failure_is_sanitized_without_fallback_or_admin_reads(
    selected: _SecretHarness,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    failed_index: int,
) -> None:
    # A legacy mount would let an accidental fallback succeed and expose the bug.
    mounts = tmp_path / "mounts"
    mounts.mkdir()
    for name in (*_RUNTIME_NAMES, "lychd_db_password", "lychd_phoenix_db_password"):
        (mounts / name).write_text("fallback-value-" + "x" * 40, encoding="utf-8")
    selected.fail_name = _RUNTIME_NAMES[failed_index]
    with pytest.raises(ValueError, match="Selected Podman runtime credentials are unavailable") as error:
        Settings()
    output = capsys.readouterr()
    emitted = "".join(traceback.format_exception(error.value)) + caplog.text + output.out + output.err
    for value in selected.values.values():
        assert value.get_secret_value() not in emitted
    assert selected.tools == ["podman"]
    assert selected.adapters == ["/usr/bin/podman"]
    assert selected.reads == list(_RUNTIME_NAMES[: failed_index + 1])


def test_loaded_generation_is_cached_and_snapshot_materialization_never_reopens_sources(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
) -> None:
    original_values = dict(selected.values)
    loaded = get_settings()
    snapshot = SettingsSnapshot.capture(loaded)
    selected.values = {name: SecretStr("fresh-generation-" + name + "x" * 40) for name in _RUNTIME_NAMES}
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", "changed-invalid-selection")
    monkeypatch.setenv("LYCHD_DB_PASSWORD", "late-override")
    assert get_settings() is loaded
    for generation in (loaded, get_settings(), snapshot.materialize(), snapshot.materialize()):
        _assert_runtime_values(generation, original_values, _RUNTIME_NAMES)
    assert selected.reads == list(_RUNTIME_NAMES)
    assert selected.tools == ["podman"]

    with pytest.raises(ValueError, match="Unsupported LYCHD_RUNTIME_SECRETS"):
        Settings()
    assert selected.reads == list(_RUNTIME_NAMES)
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", "podman-core@1")
    monkeypatch.delenv("LYCHD_DB_PASSWORD")
    fresh = Settings()
    _assert_runtime_values(fresh, selected.values, _RUNTIME_NAMES)
    assert selected.reads == [*_RUNTIME_NAMES, *_RUNTIME_NAMES]
    assert selected.tools == ["podman", "podman"]
    assert selected.adapters == ["/usr/bin/podman", "/usr/bin/podman"]
    assert get_settings() is loaded


def test_selected_values_stay_excluded_from_repr_exports_snapshots_and_logs(
    selected: _SecretHarness,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loaded = Settings()
    snapshot = SettingsSnapshot.capture(loaded)
    output = capsys.readouterr()
    exports = (
        repr(loaded),
        repr(snapshot),
        loaded.model_dump_json(),
        snapshot.payload,
        caplog.text,
        output.out,
        output.err,
    )
    for value in selected.values.values():
        assert all(value.get_secret_value() not in text for text in exports)
    exported: dict[str, Any] = loaded.model_dump()["server"]
    assert "runtime_password" not in exported["database"]
    assert "secret_key" not in exported["web"]
    assert "access_password" not in exported["web"]
    _assert_runtime_values(snapshot.materialize(), selected.values, _RUNTIME_NAMES)
    assert selected.reads == list(_RUNTIME_NAMES)


@pytest.mark.parametrize("alias", [None, "LYCHD_DB_PASSWORD", "LYCHD_APP_SECRET_KEY_FILE"])
def test_selected_source_refuses_filtering_that_would_erase_empty_overrides(
    monkeypatch: pytest.MonkeyPatch,
    selected: _SecretHarness,
    alias: str | None,
) -> None:
    if alias is not None:
        monkeypatch.setenv(alias, "")
    options: dict[str, Any] = {"_env_ignore_empty": True}
    with pytest.raises(ValueError, match="require preserved empty environment values"):
        Settings(**options)
    _assert_no_podman(selected)


def test_explicitly_discarded_empty_selection_preserves_legacy_without_podman(
    monkeypatch: pytest.MonkeyPatch,
    secrets: _SecretHarness,
) -> None:
    monkeypatch.setenv("LYCHD_RUNTIME_SECRETS", "")
    monkeypatch.setenv("LYCHD_RUNTIME_DB_PASSWORD", "legacy-runtime")
    options: dict[str, Any] = {"_env_ignore_empty": True}
    loaded = Settings(**options)
    assert loaded.server.database.runtime_password == SecretStr("legacy-runtime")
    _assert_no_podman(secrets)


def test_converted_none_selection_is_not_an_exact_profile_string(
    selected: _SecretHarness,
) -> None:
    options: dict[str, Any] = {"_env_parse_none_str": "podman-core@1"}
    with pytest.raises(ValueError, match="Unsupported LYCHD_RUNTIME_SECRETS"):
        Settings(**options)
    _assert_no_podman(selected)
