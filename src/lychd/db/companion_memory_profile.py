"""Explicit local recipe-store assembly; no service activation or schema operation."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, field_validator

from lychd.compositions.blockworld.companion import CompanionDocument, canonical_uuid, strict_json
from lychd.config.settings.server import DatabaseSettings
from lychd.db.companion_memory import DbCompanionRecipeStore
from lychd.db.factory import create_db_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from lychd.system.services.companion_bridge import BridgeBinding


class CompanionMemoryProfile(CompanionDocument):
    """A private local owner and prepared database; credentials never enter namespace bytes."""

    version: Literal[1] = 1
    owner_id: str
    host: Literal["127.0.0.1", "::1"]
    port: int = Field(ge=1, le=65535)
    user: str = Field(min_length=1, max_length=63)
    database: str = Field(min_length=1, max_length=63)
    password: SecretStr = Field(repr=False, exclude=True)
    _owner = field_validator("owner_id")(canonical_uuid)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            msg = "Memory profile version requires original integer 1."
            raise ValueError(msg)
        return value

    @field_validator("password")
    @classmethod
    def bounded_secret(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 256:  # noqa: PLR2004 - connection credential bound
            msg = "Invalid memory credential length."
            raise ValueError(msg)
        return value


def open_recipe_store(document: bytes, binding: BridgeBinding) -> tuple[AsyncEngine, DbCompanionRecipeStore]:
    """Create a lazy pool through Phylactery; caller owns disposal, migration stays explicit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    profile = CompanionMemoryProfile.model_validate(strict_json(document))
    settings = DatabaseSettings(
        host=profile.host,
        port=profile.port,
        runtime_user=profile.user,
        database=profile.database,
        runtime_password=profile.password,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
    )
    namespace = hashlib.sha256(
        json.dumps(
            {"revision": 1, "owner": profile.owner_id, "endpoint": binding.endpoint, "actor": binding.actor_id},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    engine = create_db_engine(settings, runtime=True)
    return engine, DbCompanionRecipeStore(async_sessionmaker(engine, expire_on_commit=False), namespace)
