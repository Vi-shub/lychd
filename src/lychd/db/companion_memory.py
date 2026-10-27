"""Private Composition recipe memory; the host injects one local PostgreSQL factory.

This is not an Archive profile or an authorization source. A namespace is a host-
reviewed SHA256 binding of the local owner, server and actor. This module never
constructs a database URL, changes credentials, provisions schema or retries work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import select, text

from lychd.compositions.blockworld.companion_memory import (
    MAX_RECIPES,
    CommandRecipe,
    phrase_key,
    recipe_key,
)
from lychd.db.models.companion_memory import CompanionRecipe

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from lychd.compositions.blockworld.companion_memory import DialogueLanguage

_HEX = re.compile(r"[0-9a-f]{64}")
_MAX_DOCUMENT_BYTES = 4096


class RecipeStoreError(RuntimeError):
    """A fixed non-content failure; callers must not convert this to a recall miss."""


class _RecipeRefusedError(ValueError):
    """A known conflict/capacity refusal preserves the RecipeStore value-error contract."""


def _recipe_bytes(recipe: CommandRecipe) -> bytes:
    payload = json.dumps(
        recipe.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 0 < len(payload) <= _MAX_DOCUMENT_BYTES:
        msg = "Recipe document exceeds the storage bound."
        raise ValueError(msg)
    return payload


class DbCompanionRecipeStore:
    """Exact reviewed aliases in one host-bound, private local PostgreSQL namespace.

    The host owns local-only connection and runtime-role admission. The injected
    factory must create a fresh session per call. Recall reconstructs a closed
    document; it supplies neither consent nor permission to issue a game effect.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], namespace: str) -> None:
        """Bind a fresh-session factory and one immutable namespace."""
        if type(namespace) is not str or _HEX.fullmatch(namespace) is None:
            msg = "Recipe namespace must be a lowercase SHA256 hex digest."
            raise ValueError(msg)
        self._factory = factory
        self._namespace = namespace
        # Domain separation avoids accidental coupling with other advisory users.
        # Truncation collisions only serialize unrelated namespaces; row predicates
        # always retain the complete namespace and exact recipe key.
        lock_digest = hashlib.sha256(b"lychd:companion-recipes:v1\0" + bytes.fromhex(namespace)).digest()
        self._lock_id = int.from_bytes(lock_digest[:8], byteorder="big", signed=True)

    @asynccontextmanager
    async def _transaction(self, *, write: bool) -> AsyncGenerator[AsyncSession]:
        commit_started = False
        try:
            async with self._factory() as session:
                if session.get_bind().dialect.name != "postgresql":
                    msg = "recipe_postgresql_required"
                    raise RecipeStoreError(msg)  # noqa: TRY301 - preserve the fixed dialect refusal
                async with session.begin():
                    await session.execute(text("SET LOCAL statement_timeout = '5s'"))
                    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                    # A repeatable-read snapshot acquired while waiting for the
                    # advisory lock could otherwise leave a stale capacity view.
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                    if write:
                        await session.execute(
                            text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS BIGINT))"),
                            {"lock_id": self._lock_id},
                        )
                    yield session
                    # Every mutation flushes within the body. Errors from this
                    # point may mean COMMIT succeeded but its response was lost.
                    commit_started = write
        except (RecipeStoreError, _RecipeRefusedError):
            raise
        except Exception:  # noqa: BLE001 - suppress private driver diagnostics across the storage boundary
            msg = "recipe_commit_unknown" if commit_started else "recipe_storage_failure"
            raise RecipeStoreError(msg) from None
        # Cancellation remains cancellation, including during COMMIT. Its write
        # outcome must be reconciled by an explicit read; never replay it blindly.

    def _decode(self, row: CompanionRecipe) -> CommandRecipe:
        try:
            if (
                row.namespace != self._namespace
                or type(row.key) is not str
                or _HEX.fullmatch(row.key) is None
                or type(row.language) is not str
                or row.language not in ("sk", "cs", "en")
                or type(row.phrase) is not str
                or row.phrase != phrase_key(row.phrase)
                or type(row.recipe_bytes) is not bytes
                or not 0 < len(row.recipe_bytes) <= _MAX_DOCUMENT_BYTES
                or type(row.recipe_digest) is not str
                or _HEX.fullmatch(row.recipe_digest) is None
                or not isinstance(cast("object", row.learned_request_id), UUID)
                or not isinstance(cast("object", row.learned_run_id), UUID)
            ):
                raise ValueError  # noqa: TRY301 - map corrupt rows to the same content-free error
            actual_digest = hashlib.sha256(row.recipe_bytes).hexdigest()
            if not hmac.compare_digest(row.recipe_digest, actual_digest):
                raise ValueError  # noqa: TRY301 - map corrupt rows to the same content-free error
            recipe = CommandRecipe.model_validate_json(row.recipe_bytes)
            if (
                recipe.language != row.language
                or recipe.phrase != row.phrase
                or recipe_key(recipe.language, recipe.phrase) != row.key
                or recipe.learned_request_id != str(row.learned_request_id)
                or recipe.learned_run_id != str(row.learned_run_id)
                or _recipe_bytes(recipe) != row.recipe_bytes
            ):
                raise ValueError  # noqa: TRY301 - map corrupt rows to the same content-free error
        except (ValueError, TypeError, AttributeError):
            msg = "recipe_integrity"
            raise RecipeStoreError(msg) from None
        return recipe

    async def _load(self, session: AsyncSession) -> dict[str, tuple[CompanionRecipe, CommandRecipe]]:
        # A finite full namespace read also detects corruption or excess capacity
        # before an operation can treat an unrelated damaged record as absence.
        rows = (
            await session.scalars(
                select(CompanionRecipe)
                .where(CompanionRecipe.namespace == self._namespace)
                .order_by(CompanionRecipe.key)
                .limit(MAX_RECIPES + 1)
            )
        ).all()
        if len(rows) > MAX_RECIPES:
            msg = "recipe_integrity"
            raise RecipeStoreError(msg)
        found: dict[str, tuple[CompanionRecipe, CommandRecipe]] = {}
        for row in rows:
            recipe = self._decode(row)
            if row.key in found:
                msg = "recipe_integrity"
                raise RecipeStoreError(msg)
            found[row.key] = row, recipe
        return found

    async def find(self, language: DialogueLanguage, phrase: str) -> CommandRecipe | None:
        if type(language) is not str or language not in ("sk", "cs", "en"):
            msg = "Recipe language must be sk, cs or en."
            raise ValueError(msg)
        key = recipe_key(language, phrase)
        async with self._transaction(write=False) as session:
            found = await self._load(session)
            previous = found.get(key)
            result = None if previous is None else previous[1]
        return result  # noqa: RET504 - disclose only after session cleanup

    async def remember(self, recipe: CommandRecipe) -> None:
        checked = CommandRecipe.model_validate(recipe.model_dump(mode="python"))
        payload = _recipe_bytes(checked)
        key = recipe_key(checked.language, checked.phrase)
        async with self._transaction(write=True) as session:
            found = await self._load(session)
            previous = found.get(key)
            if previous is not None:
                if _recipe_bytes(previous[1]) != payload:
                    msg = "Forget the previous recipe before changing it."
                    raise _RecipeRefusedError(msg)
                return
            if len(found) >= MAX_RECIPES:
                msg = "Recipe capacity reached."
                raise _RecipeRefusedError(msg)
            session.add(
                CompanionRecipe(
                    namespace=self._namespace,
                    key=key,
                    language=checked.language,
                    phrase=checked.phrase,
                    recipe_bytes=payload,
                    recipe_digest=hashlib.sha256(payload).hexdigest(),
                    learned_request_id=UUID(checked.learned_request_id),
                    learned_run_id=UUID(checked.learned_run_id),
                )
            )
            await session.flush()

    async def forget(self, language: DialogueLanguage, phrase: str) -> bool:
        if type(language) is not str or language not in ("sk", "cs", "en"):
            msg = "Recipe language must be sk, cs or en."
            raise ValueError(msg)
        key = recipe_key(language, phrase)
        async with self._transaction(write=True) as session:
            found = await self._load(session)
            previous = found.get(key)
            if previous is None:
                result = False
            else:
                await session.delete(previous[0])
                await session.flush()
                result = True
        return result
