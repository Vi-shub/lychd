"""Closed local candidate authority; attribution never conveys an effect permit.

The host alone composes enrollment and assignment methods. The protected publication
is synchronous and must perform no blocking I/O, await, or external effect. Preparation
runs before the store acquires its durable serialization boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

AUDIENCE = "host-opaque-candidate@1"


class WardRefusedError(Exception):
    """No credential, session or object authority is available."""


class PrincipalKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    PEER = "peer"
    NODE = "node"


class Action(StrEnum):
    INSPECT = "candidate:inspect"
    ADMIT = "candidate:admit"
    READ = "candidate:read"
    DECIDE = "candidate:decide"


@dataclass(frozen=True, slots=True)
class Secret:
    """One high-entropy bearer value; repr is deliberately secret-free."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject inputs outside this closed local profile."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.value):
            message = "Ward secret has an invalid shape."
            raise ValueError(message)

    @classmethod
    def generate(cls) -> Secret:
        return cls(secrets.token_urlsafe(32))

    def digest(self) -> str:
        return hashlib.sha256(self.value.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class Operation:
    action: Action
    object_id: UUID
    purpose: str
    audience: str = AUDIENCE

    def __post_init__(self) -> None:
        """Reject inputs outside this closed local profile."""
        if (
            type(self.action) is not Action
            or type(self.object_id) is not UUID
            or not re.fullmatch(r"[a-z][a-z0-9:._-]{0,95}", self.purpose)
            or self.audience != AUDIENCE
        ):
            message = "Ward operation is outside the local candidate profile."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Attribution:
    """Historical identity and generations, explicitly insufficient for authorization."""

    principal_id: UUID
    credential_id: UUID
    session_id: UUID
    principal_generation: int
    credential_generation: int
    session_generation: int
    policy_generation: int
    publication_attempt_id: UUID


@dataclass(frozen=True, slots=True)
class Enrollment:
    principal_id: UUID
    user_id: UUID | None
    credential_id: UUID
    credential: Secret


@dataclass(frozen=True, slots=True)
class Authentication:
    session_id: UUID
    principal_id: UUID
    secret: Secret


def require_generation(value: int) -> None:
    """Require the exact admitted policy generation for deferred work."""
    if type(value) is not int or value < 1:
        message = "Ward policy generation must be a positive integer."
        raise ValueError(message)


def require_synchronous(publication: object) -> None:
    """Refuse detectable continuations before any preparation or store work."""
    if not callable(publication) or any(
        inspect.iscoroutinefunction(candidate)
        or inspect.isasyncgenfunction(candidate)
        or inspect.isgeneratorfunction(candidate)
        for candidate in (publication, publication.__call__)
    ):
        message = "Ward publication must be synchronous."
        raise TypeError(message)


def handoff[T](publication: Callable[[T, Attribution], None], prepared: T, context: Attribution) -> None:
    """Dispose detectable asynchronous returns before yielding; retain ambiguity."""
    result = cast("object", publication(prepared, context))
    if result is None:
        return
    if inspect.iscoroutine(result):
        result.close()
    elif isinstance(result, asyncio.Future):
        result.cancel()
    message = "Ward publication must return None; its outcome is unknown."
    raise TypeError(message)


class WardStore(Protocol):
    async def enroll(self, kind: PrincipalKind, *, expires_at: datetime) -> Enrollment: ...

    async def open_session(self, credential: Secret, *, audience: str, expires_at: datetime) -> Authentication: ...

    async def assign(self, principal_id: UUID, operation: Operation, *, expires_at: datetime, allowed: bool) -> int: ...

    async def revoke(self, principal_id: UUID, *, credential_id: UUID | None, session_id: UUID | None) -> None: ...

    async def publish[T](
        self,
        session_secret: Secret,
        operation: Operation,
        prepared: T,
        publication: Callable[[T, Attribution], None],
        *,
        expected_policy_generation: int,
    ) -> None: ...


class WardService:
    """Explicit host composition seam, with no app registration or permissive default."""

    def __init__(self, store: WardStore) -> None:
        """Require an explicitly supplied authority store."""
        self._store = store

    async def enroll(self, kind: PrincipalKind, *, expires_at: datetime) -> Enrollment:
        return await self._store.enroll(kind, expires_at=expires_at)

    async def open_session(
        self, credential: Secret, *, expires_at: datetime, audience: str = AUDIENCE
    ) -> Authentication:
        return await self._store.open_session(credential, audience=audience, expires_at=expires_at)

    async def assign(
        self, principal_id: UUID, operation: Operation, *, expires_at: datetime, allowed: bool = True
    ) -> int:
        return await self._store.assign(principal_id, operation, expires_at=expires_at, allowed=allowed)

    async def revoke(
        self, principal_id: UUID, *, credential_id: UUID | None = None, session_id: UUID | None = None
    ) -> None:
        await self._store.revoke(principal_id, credential_id=credential_id, session_id=session_id)

    async def publish[T](
        self,
        session_secret: Secret,
        operation: Operation,
        *,
        prepare: Callable[[], Awaitable[T]],
        publication: Callable[[T, Attribution], None],
        expected_policy_generation: int,
    ) -> None:
        """Finish preparation before fresh authority; never return a live Grant.

        Publication must hand off the projection itself. Returning protected bytes
        for later disclosure would cross an await after the final authority check.
        """
        require_generation(expected_policy_generation)
        require_synchronous(publication)
        prepared = await prepare()
        await self._store.publish(
            session_secret,
            operation,
            prepared,
            publication,
            expected_policy_generation=expected_policy_generation,
        )
