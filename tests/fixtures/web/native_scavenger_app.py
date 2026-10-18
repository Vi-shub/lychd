"""Narrow opt-in retained Scavenger intake for the native-manager proof.

The external PostgreSQL resource remains Root-owned and live throughout. Only the
native Vessel belongs to this proof's Scribe catalog. This is production assembly
through the unchanged CLI, with no DeveloperFixture or application substitution.
Application filesystem roots are explicit, pre-created Root inputs; this helper
validates them without creating or repairing directories or recovery markers.
The inherited private bus addresses remain separate from application XDG paths.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

if TYPE_CHECKING:
    from httpx import Client

    from lychd.system.services.local_access import LocalAccess

_TABLES = ("scout_budget", "scout_attempt", "scavenger_observation", "run", "run_delivery")
_DIRECTORY_KEYS = {
    "codex_root",
    "application_home",
    "xdg_data_home",
    "xdg_cache_home",
    "xdg_state_home",
    "xdg_runtime_dir",
}
_FILE_KEYS = {
    "runtime_password_file",
    "access_password_file",
    "app_secret_file",
    "canonical_document",
    "accounting_document",
}
_KEYS = _DIRECTORY_KEYS | _FILE_KEYS | {"admission_id"}
_MAX_BYTES = 2 * 1024 * 1024


def _private_file(path: Path) -> bytes:
    assert path.is_absolute()
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_size <= _MAX_BYTES
    with path.open("rb") as stream:
        value = stream.read(_MAX_BYTES + 1)
    assert len(value) <= _MAX_BYTES
    return value


def _private_directory(path: Path) -> tuple[int, int]:
    assert path.is_absolute()
    assert path.resolve(strict=True) == path
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700
    return metadata.st_dev, metadata.st_ino


def _finite_json(value: object) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode(
        "utf-8"
    )
    assert len(encoded) <= _MAX_BYTES, "retained JSON exceeds finite artifact bound"
    return encoded


def _object(payload: bytes) -> dict[str, Any]:
    value: object = json.loads(payload)
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


@dataclass
class RetainedScavenger:
    """One explicit private input set; credential bytes never enter a receipt."""

    profile: dict[str, str]
    port: int
    pinned: dict[Path, bytes] = field(repr=False)
    directories: dict[Path, tuple[int, int]] = field(repr=False)
    declaration: dict[str, Any] = field(repr=False)
    expected_document: dict[str, Any] = field(repr=False)
    expected_accounting: dict[str, Any] = field(repr=False)

    @classmethod
    def load(cls, path: Path, *, port: int) -> RetainedScavenger:
        """Read only after the caller attests its manager and full-bus peer."""
        payload = _private_file(path)
        values = _object(payload)
        assert all(isinstance(value, str) for value in values.values())
        profile = cast("dict[str, str]", values)
        assert set(profile) == _KEYS
        assert str(UUID(profile["admission_id"])) == profile["admission_id"]
        codex = Path(profile["codex_root"])
        assert codex.name == "lychd"
        _private_directory(codex)
        pinned = {path: payload}
        for key in _FILE_KEYS:
            target = Path(profile[key])
            pinned[target] = _private_file(target)
        config = codex / "lychd.toml"
        pinned[config] = _private_file(config)
        # The selected retained feed needs no model or external extension organ.
        runes = codex / "runes"
        assert not runes.is_symlink()
        assert not list(runes.rglob("*.toml")), "this finite proof accepts no Rune runtime"
        declaration = tomllib.loads(pinned[config].decode("utf-8"))
        crypt = Path(profile["xdg_data_home"]) / "lychd"
        paths = {Path(profile[key]) for key in _DIRECTORY_KEYS}
        paths.update((codex.parent, runes, crypt, crypt / "runtime-guard"))
        switching = declaration.get("orchestration", {}).get("switching", {})
        if switching.get("actuator", "host-reactor") == "host-reactor":
            inbox = crypt / "triggers/inbox"
            assert Path(switching.get("host_reactor_dir", str(inbox))) == inbox, (
                "application data/Reactor path mismatch"
            )
            paths.update((inbox.parent, inbox, inbox.parent / "journal"))
        directories = {directory: _private_directory(directory) for directory in paths}
        assert declaration["server"]["database"].get("profile", "postgres") == "postgres"
        assert declaration["weaver"]["scavenger"]["revision"] == "3"
        declaration.setdefault("server", {})["port"] = port
        canonical = _object(pinned[Path(profile["canonical_document"])])
        accounting = _object(pinned[Path(profile["accounting_document"])])
        assert canonical.get("status") == "observed"
        assert canonical["terminal_disposition"] == "terminal"
        assert len(canonical["observation"]["items"]) == 50
        assert set(accounting) == set(_TABLES)
        _finite_json(canonical)
        _finite_json(accounting)
        return cls(profile, port, pinned, directories, declaration, canonical, accounting)

    def unchanged(self) -> None:
        for path, identity in self.directories.items():
            assert _private_directory(path) == identity, "application directory identity changed"
        for path, value in self.pinned.items():
            if _private_file(path) != value:
                message = "retained proof input changed"
                raise AssertionError(message)
        assert not list((Path(self.profile["codex_root"]) / "runes").rglob("*.toml"))

    def _secret(self, name: str) -> str:
        return self.pinned[Path(self.profile[name])].decode("utf-8").strip()

    def access(self) -> LocalAccess:
        from pydantic import SecretStr

        from lychd.system.services.local_access import LocalAccess

        return LocalAccess(
            url=f"http://127.0.0.1:{self.port}/", password=SecretStr(self._secret("access_password_file"))
        )

    def accounting(self) -> dict[str, Any]:
        """Compare complete canonical/accounting rows under data-only read authority."""
        import psycopg
        from psycopg import sql
        from pydantic import SecretStr

        from lychd.config.settings.root import SettingsSnapshot
        from lychd.db.authority import RUNTIME_AUTHORITY_QUERY, require_runtime_authority
        from lychd.db.factory import database_saq_dsn

        self.unchanged()
        settings = SettingsSnapshot(
            payload=json.dumps(self.declaration),
            database_runtime_password=SecretStr(self._secret("runtime_password_file")),
        ).materialize()
        observed: dict[str, Any] = {}
        with (
            psycopg.connect(database_saq_dsn(settings.server.database, runtime=True), connect_timeout=5) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute(RUNTIME_AUTHORITY_QUERY)
            row = cursor.fetchone()
            require_runtime_authority(row[0] if row else None)
            for table in _TABLES:
                cursor.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} AS t ORDER BY id LIMIT 101").format(sql.Identifier(table))
                )
                rows = [row[0] for row in cursor.fetchall()]
                assert len(rows) <= 100, "retained profile exceeds finite row bound"
                observed[table] = rows
            cursor.execute("SELECT count(*) FROM saq_jobs WHERE status NOT IN ('complete','failed','aborted')")
            assert cursor.fetchone() == (0,), "actionable SAQ work prevents retained-only startup"
        assert all(row["status"] in {"done", "failed", "cancelled"} for row in observed["run"])
        assert all(row["state"] == "settled" for row in observed["run_delivery"])
        assert all(row["status"] in {"succeeded", "refused"} for row in observed["scout_attempt"])
        assert {row["scope"] for row in observed["scout_budget"]} == {"host", "binding"}
        for row in observed["scout_budget"]:
            assert row["active_claims"] == 0
            assert all(row[f"{name}_reserved"] == 0 for name in ("connects", "transmissions", "wire_bytes"))
        assert _finite_json(observed) == _finite_json(self.expected_accounting), "canonical/accounting rows changed"
        return observed

    def document(self, client: Client) -> dict[str, Any]:
        self.unchanged()
        response = client.get(
            f"http://127.0.0.1:{self.port}/api/v1/scavenger/observations/{self.profile['admission_id']}",
            auth=("magus", self._secret("access_password_file")),
        )
        assert response.status_code == 200
        observed = cast("dict[str, Any]", response.json())
        assert _finite_json(observed) == _finite_json(self.expected_document), (
            "complete retained Scavenger document changed"
        )
        return observed


def main(config_path: Path) -> None:
    """Set isolated source/file references before importing any application module."""
    config = json.loads(_private_file(config_path))
    assert set(config) == {"profile_path", "port"}
    assert sys.argv[1:] == ["serve", "--host", "127.0.0.1", "--port", str(config["port"])]
    retained = RetainedScavenger.load(Path(config["profile_path"]), port=config["port"])
    os.chdir(config_path.parent)
    private_buses = {key: os.environ[key] for key in ("DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS")}
    application_environment = {
        "HOME": retained.profile["application_home"],
        "XDG_CONFIG_HOME": str(Path(retained.profile["codex_root"]).parent),
        "XDG_DATA_HOME": retained.profile["xdg_data_home"],
        "XDG_CACHE_HOME": retained.profile["xdg_cache_home"],
        "XDG_STATE_HOME": retained.profile["xdg_state_home"],
        "XDG_RUNTIME_DIR": retained.profile["xdg_runtime_dir"],
    }
    os.environ.update(
        {
            **application_environment,
            "SERVER__PORT": str(retained.port),
            "LYCHD_RUNTIME_DB_PASSWORD_FILE": retained.profile["runtime_password_file"],
            "LYCHD_LOCAL_ACCESS_PASSWORD_FILE": retained.profile["access_password_file"],
            "LYCHD_APP_SECRET_KEY_FILE": retained.profile["app_secret_file"],
        }
    )
    assert {key: os.environ[key] for key in private_buses} == private_buses
    source = Path(globals()["SOURCE_ROOT"]).resolve(strict=True)
    assert sys.dont_write_bytecode
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    assert Path(sys.path[0]).resolve(strict=True) == source
    from lychd.__main__ import cli

    package_file = sys.modules["lychd"].__file__
    cli_file = sys.modules["lychd.__main__"].__file__
    assert package_file is not None
    assert cli_file is not None
    assert Path(package_file).resolve(strict=True) == source / "lychd/__init__.py"
    assert Path(cli_file).resolve(strict=True) == source / "lychd/__main__.py"
    event = {
        "event": "source-loaded",
        "pid": os.getpid(),
        "invocation_id": os.environ["INVOCATION_ID"],
        "source_root": str(source),
        "application_environment": {key: os.environ[key] for key in application_environment},
        "private_bus_environment": private_buses,
        "package_origin": str(Path(package_file).resolve(strict=True)),
        "entrypoint_origin": str(Path(cli_file).resolve(strict=True)),
        "dont_write_bytecode": sys.dont_write_bytecode,
        "bytecode_flag": bool(sys.flags.dont_write_bytecode),
        "unbuffered": bool(getattr(sys.stdout, "write_through", False)),
    }
    payload = b"LYCHD_NATIVE_PROOF " + _finite_json(event) + b"\n"
    assert os.write(1, payload) == len(payload)
    cli.main(args=sys.argv[1:], prog_name="lychd")


if __name__ == "__main__":
    main(Path(globals()["CONFIG_PATH"]))
