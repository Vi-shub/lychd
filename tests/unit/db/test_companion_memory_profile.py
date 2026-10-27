"""Lazy local pool assembly; no connection or database service is used."""

from __future__ import annotations

import json
from typing import Any

import pytest

from lychd.db import companion_memory_profile
from lychd.system.services.companion_bridge import BridgeBinding


def document(**changes: Any) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "owner_id": "00000000-0000-4000-8000-000000000001",
            "host": "127.0.0.1",
            "port": 5432,
            "user": "lychd_runtime",
            "database": "lychd",
            "password": "synthetic-unit-fixture",
            **changes,
        }
    ).encode()


def test_memory_pool_uses_verified_runtime_role_and_keeps_secret_out_of_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    engine = object()

    def create(settings: Any, *, runtime: bool) -> Any:
        calls.append((settings, runtime))
        return engine

    monkeypatch.setattr(companion_memory_profile, "create_db_engine", create)
    binding = BridgeBinding(
        endpoint="http://127.0.0.1:18184", token="ab" * 32, actor_id="00000000-0000-4000-8000-000000000002"
    )
    actual, store = companion_memory_profile.open_recipe_store(document(), binding)
    assert actual is engine
    assert calls[0][1] is True
    assert calls[0][0].runtime_user == "lychd_runtime"
    assert calls[0][0].password is None
    assert calls[0][0].runtime_password.get_secret_value() == "synthetic-unit-fixture"
    _, changed_password = companion_memory_profile.open_recipe_store(document(password="another-fixture"), binding)  # noqa: S106 - synthetic unit credential
    assert store._namespace == changed_password._namespace  # pyright: ignore[reportPrivateUsage]
    assert "synthetic-unit-fixture" not in repr(store)


@pytest.mark.parametrize(
    "changes", [{"host": "example.org"}, {"version": True}, {"user": "lich"}, {"user": "lychd_phoenix"}]
)
def test_invalid_or_administrative_profile_is_rejected_before_pool_construction(
    changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid profile must not construct a pool")

    monkeypatch.setattr(companion_memory_profile, "create_db_engine", forbidden)
    binding = BridgeBinding(
        endpoint="http://127.0.0.1:18184", token="ab" * 32, actor_id="00000000-0000-4000-8000-000000000002"
    )
    with pytest.raises(ValueError, match="validation"):
        companion_memory_profile.open_recipe_store(document(**changes), binding)
