"""Startup-only adaptation of core credentials from the selected runtime source."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast, override

from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource

from lychd.config.settings.server import ServerSettings

MOUNTED_SECRETS_DIR = Path("/run/secrets")


@dataclass(frozen=True, slots=True)
class RuntimeSecretCapture:
    """Non-secret provenance of one successful selected startup capture."""

    profile: str
    names: tuple[str, ...]


@dataclass(slots=True)
class _CaptureScope:
    result: RuntimeSecretCapture | None = None


_CAPTURE_SCOPE: ContextVar[_CaptureScope | None] = ContextVar("runtime_secret_capture", default=None)


@contextmanager
def runtime_secret_capture() -> Generator[_CaptureScope]:
    """Isolate provenance for nested and concurrent Settings constructions."""
    scope = _CaptureScope()
    token = _CAPTURE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CAPTURE_SCOPE.reset(token)


class RuntimeSecretsSource(PydanticBaseSettingsSource):
    """Capture legacy credentials or explicitly selected Podman runtime credentials."""

    def __init__(self, settings_cls: type[BaseSettings], environment: PydanticBaseSettingsSource) -> None:
        """Reuse the environment captured by Pydantic for this construction."""
        super().__init__(settings_cls)
        if not isinstance(environment, EnvSettingsSource):
            msg = "Runtime secrets require Pydantic's environment source."
            raise TypeError(msg)
        self._environment = environment.env_vars
        self._case_sensitive = environment.case_sensitive
        self._ignore_empty = environment.env_ignore_empty

    @override
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    @override
    def __call__(self) -> dict[str, Any]:
        key = "LYCHD_RUNTIME_SECRETS" if self._case_sensitive else "lychd_runtime_secrets"
        if key in self._environment:
            selection = self._environment[key]
            if type(selection) is not str or selection != "podman-core@1":
                msg = "Unsupported LYCHD_RUNTIME_SECRETS selection."
                raise ValueError(msg)
            if self._ignore_empty:
                msg = "Selected Podman runtime credentials require preserved empty environment values."
                raise ValueError(msg)
            self._reject_podman_overrides()
            return self._podman_core_credentials()
        return self._legacy_credentials()

    def _legacy_credentials(self) -> dict[str, Any]:
        """Retain the established explicit, environment and mounted-file precedence."""
        self._reject_toml_credentials()
        declared = self.current_state.get("server", {})
        server = ServerSettings.model_validate(declared)
        if isinstance(declared, BaseModel):
            return {}  # Explicit section instances already supply their complete values.

        credentials: dict[str, dict[str, SecretStr]] = {}
        for section, field, env_key, secret_name in (
            ("database", "password", "LYCHD_DB_PASSWORD", server.database.password_secret),
            ("database", "runtime_password", "LYCHD_RUNTIME_DB_PASSWORD", server.database.runtime_password_secret),
            ("database", "phoenix_password", "LYCHD_PHOENIX_DB_PASSWORD", server.database.phoenix_password_secret),
            ("web", "secret_key", "LYCHD_APP_SECRET_KEY", server.web.secret_key_secret),
            ("web", "access_password", "LYCHD_LOCAL_ACCESS_PASSWORD", server.web.access_password_secret),
        ):
            existing = declared.get(section, {})
            if isinstance(existing, BaseModel) or field in existing:
                continue
            key = env_key if self._case_sensitive else env_key.lower()
            value = self._environment.get(key)
            if not value:
                file_key = f"{key}_FILE" if self._case_sensitive else f"{key}_file"
                override_path = self._environment.get(file_key)
                path = Path(override_path) if override_path else MOUNTED_SECRETS_DIR / secret_name
                try:
                    value = path.read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    if not override_path:
                        continue  # Host bootstrap can precede secret provisioning.
                    msg = f"Secret file for {env_key} is unavailable: '{path}'."
                    raise ValueError(msg) from None
                except (OSError, UnicodeError):
                    msg = f"Secret file for {env_key} cannot be read: '{path}'."
                    raise ValueError(msg) from None
                if not value:
                    msg = f"Secret file for {env_key} is empty: '{path}'."
                    raise ValueError(msg)
            credentials.setdefault(section, {})[field] = SecretStr(value)
        return {"server": credentials}

    def _reject_podman_overrides(self) -> None:
        """Refuse constructor server sections and supplied values before secret I/O."""
        msg = "LYCHD_RUNTIME_SECRETS=podman-core@1 forbids supplied core credentials or constructor server sections."
        if "server" in self.settings_sources_data.get("InitSettingsSource", {}):
            raise ValueError(msg)
        for env_key in (
            "LYCHD_DB_PASSWORD",
            "LYCHD_RUNTIME_DB_PASSWORD",
            "LYCHD_PHOENIX_DB_PASSWORD",
            "LYCHD_APP_SECRET_KEY",
            "LYCHD_LOCAL_ACCESS_PASSWORD",
        ):
            for alias in (env_key, f"{env_key}_FILE"):
                key = alias if self._case_sensitive else alias.lower()
                if key in self._environment:
                    raise ValueError(msg)

        # Earlier source states retain competing inputs hidden by precedence.
        if any(
            self._has_supplied_credentials(values.get("server", {}))
            for values in (*self.settings_sources_data.values(), self.current_state)
        ):
            raise ValueError(msg)

    @staticmethod
    def _has_supplied_credentials(declared: object) -> bool:
        """Identify credential inputs and complete sections without reading their values."""
        if isinstance(declared, BaseModel):
            return True
        if not isinstance(declared, dict):
            return False  # ServerSettings validation rejects malformed declarations.
        server = cast("dict[str, Any]", declared)
        for section, fields in (
            ("database", ("password", "runtime_password", "phoenix_password")),
            ("web", ("secret_key", "access_password")),
        ):
            existing = server.get(section, {})
            if isinstance(existing, BaseModel) or (
                isinstance(existing, dict) and any(field in existing for field in fields)
            ):
                return True
        return False

    def _podman_core_credentials(self) -> dict[str, Any]:
        """Read only the declared runtime password, signing key and local access password."""
        from lychd.system.host_tools import trusted_host_tool
        from lychd.system.services.secrets import PodmanSecretStore, PodmanSecretStoreError

        server = ServerSettings.model_validate(self.current_state.get("server", {}))
        try:
            podman = trusted_host_tool("podman")
            if podman is None:
                msg = "A trusted Podman executable is required for selected runtime credentials."
                raise ValueError(msg)
            store = PodmanSecretStore(podman)
            credentials = {
                "database": {"runtime_password": store.read(server.database.runtime_password_secret)},
                "web": {
                    "secret_key": store.read(server.web.secret_key_secret),
                    "access_password": store.read(server.web.access_password_secret),
                },
            }
        except (OSError, PodmanSecretStoreError):
            msg = "Selected Podman runtime credentials are unavailable."
            raise ValueError(msg) from None
        if (scope := _CAPTURE_SCOPE.get()) is not None:
            scope.result = RuntimeSecretCapture(
                profile="podman-core@1",
                names=(
                    server.database.runtime_password_secret,
                    server.web.secret_key_secret,
                    server.web.access_password_secret,
                ),
            )
        return {"server": credentials}

    def _reject_toml_credentials(self) -> None:
        raw_server = self.settings_sources_data.get("TomlConfigSettingsSource", {}).get("server", {})
        if not isinstance(raw_server, dict):
            return  # Root validation reports malformed sections.
        toml_server = cast("dict[str, Any]", raw_server)
        for section, field in (
            ("database", "password"),
            ("database", "runtime_password"),
            ("database", "phoenix_password"),
            ("web", "secret_key"),
            ("web", "access_password"),
        ):
            section_values = toml_server.get(section)
            if isinstance(section_values, dict) and field in section_values:
                msg = f"server.{section}.{field} is a runtime credential; TOML may contain only its secret reference."
                raise ValueError(msg)
