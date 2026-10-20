"""PostgreSQL Ward profile with one permanent Principal serialization boundary.

Every admitted READ COMMITTED auth, policy, revocation and publication operation
locks that Principal. Other actual transaction isolation levels refuse before data reads.
The final exact assignment query and database clock precede synchronous
publication without a yield. This adapter creates no engine or migration authority.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import get_ident
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lychd.db.models.creation import CreationAdmission, CreationCase, CreationFeedback
from lychd.db.models.ward import WardAudit, WardCredential, WardPermission, WardPrincipal, WardSession, WardUser
from lychd.domain.ward.service import (
    AUDIENCE,
    Attribution,
    Authentication,
    Enrollment,
    Operation,
    PrincipalKind,
    Secret,
    WardRefusedError,
    handoff,
    require_generation,
    require_synchronous,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from sqlalchemy.orm import Session

PROTOCOL = "opaque-sha256@1"


def _expiry(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        message = "Ward expiry must be timezone-aware."
        raise ValueError(message)
    return value.astimezone(UTC)


def _refused() -> WardRefusedError:
    return WardRefusedError("Ward authority is unavailable.")


def _require_current(*, current: bool) -> None:
    if not current:
        raise _refused()


def _audit(
    session: AsyncSession,
    event: str,
    *,
    permitted: bool,
    principal: WardPrincipal | None = None,
    credential: WardCredential | None = None,
    authentication: WardSession | None = None,
    operation: Operation | None = None,
    attempt_id: UUID | None = None,
    outcome: str | None = None,
    expected_policy_generation: int | None = None,
    issuer_id: UUID | None = None,
) -> None:
    session.add(
        WardAudit(
            id=attempt_id or uuid4(),
            outcome=outcome,
            expected_policy_generation=expected_policy_generation,
            issuer_id=issuer_id,
            event=event,
            permitted=permitted,
            principal_id=principal.id if principal else None,
            policy_generation=principal.policy_generation if principal else None,
            principal_generation=principal.generation if principal else None,
            credential_id=credential.id if credential else None,
            credential_generation=credential.generation if credential else None,
            session_id=authentication.id if authentication else None,
            session_generation=authentication.generation if authentication else None,
            audience=operation.audience if operation else AUDIENCE,
            purpose=operation.purpose if operation else None,
            action=operation.action.value if operation else None,
            object_id=operation.object_id if operation else None,
        )
    )


class DbWardStore:
    """Explicit issuer-bound host adapter. No authority cache survives a call."""

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, issuer_id: UUID) -> None:
        """Bind the existing Phylactery factory and this installation issuer."""
        self._factory = factory
        self._issuer_id = issuer_id

    @staticmethod
    async def _admit_transaction(session: AsyncSession) -> None:
        """Require real asyncpg transactions and statement snapshots before discovery."""
        message = "Ward local profile requires actual READ COMMITTED in an asyncpg transaction."
        bind = session.bind
        models = (WardPrincipal, WardUser, WardCredential, WardSession, WardPermission, WardAudit)
        if (
            not isinstance(bind, AsyncEngine)
            or bind.dialect.name != "postgresql"
            or bind.dialect.driver != "asyncpg"
            or any(session.get_bind(mapper=model) is not bind.sync_engine for model in models)
        ):
            raise WardRefusedError(message)
        connection = await session.connection()
        sync_connection = connection.sync_connection
        if sync_connection is None:
            raise WardRefusedError(message)
        pooled = sync_connection.connection
        if getattr(pooled.dbapi_connection, "autocommit", None) is not False:
            raise WardRefusedError(message)
        isolation = await session.scalar(text("SHOW transaction_isolation"))
        # asyncpg starts its transaction before SHOW; inspect the backend status too.
        if isolation != "read committed" or cast("Any", pooled.driver_connection).is_in_transaction() is not True:
            raise WardRefusedError(message)
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        await session.execute(text("SET LOCAL lock_timeout = '2s'"))

    @staticmethod
    async def _admit_candidate_transaction(session: AsyncSession) -> None:
        """Admit native Creation reads/flushes into the very same Ward engine.

        A shared Session can own multiple physical transactions. Require both
        mapper and table resolution, including masked table overrides, before SQL.
        """
        bind = session.bind
        models = (CreationCase, CreationAdmission, CreationFeedback)
        if not isinstance(bind, AsyncEngine) or any(
            session.get_bind(mapper=model) is not bind.sync_engine
            or session.get_bind(clause=model.__table__) is not bind.sync_engine
            for model in models
        ):
            message = "Native Creation custody requires Ward's exact AsyncEngine for every mapper and table."
            raise WardRefusedError(message)
        await DbWardStore._admit_transaction(session)

    @staticmethod
    async def _now(session: AsyncSession) -> datetime:
        return cast("datetime", await session.scalar(select(func.clock_timestamp())))

    @staticmethod
    async def _principal(session: AsyncSession, principal_id: UUID) -> WardPrincipal | None:
        return await session.scalar(select(WardPrincipal).where(WardPrincipal.id == principal_id).with_for_update())

    def _credential_current(self, row: WardCredential, principal: WardPrincipal, now: datetime) -> bool:
        return (
            principal.revoked_at is None
            and row.principal_id == principal.id
            and row.issuer_id == self._issuer_id
            and row.protocol == PROTOCOL
            and row.audience == AUDIENCE
            and row.revoked_at is None
            and row.expires_at > now
        )

    async def enroll(self, kind: PrincipalKind, *, expires_at: datetime) -> Enrollment:
        """Trusted host enrollment creates no permissions or authentication Session."""
        if type(kind) is not PrincipalKind:
            message = "Ward requires an explicit Principal kind."
            raise ValueError(message)
        expiry = _expiry(expires_at)
        secret = Secret.generate()
        principal_id, credential_id = uuid4(), uuid4()
        user_id = uuid4() if kind is PrincipalKind.HUMAN else None
        async with self._factory() as session, session.begin():
            await self._admit_transaction(session)
            if expiry <= await self._now(session):
                raise _refused()
            principal = WardPrincipal(id=principal_id, kind=kind.value, generation=1, policy_generation=1)
            session.add(principal)
            await session.flush()
            credential = WardCredential(
                id=credential_id,
                principal_id=principal_id,
                issuer_id=self._issuer_id,
                protocol=PROTOCOL,
                audience=AUDIENCE,
                secret_digest=secret.digest(),
                generation=1,
                expires_at=expiry,
            )
            session.add(credential)
            if user_id is not None:
                session.add(WardUser(id=user_id, principal_id=principal_id))
            await session.flush()
            _audit(session, "enroll", permitted=True, principal=principal, credential=credential)
        return Enrollment(principal_id, user_id, credential_id, secret)

    async def open_session(self, credential: Secret, *, audience: str, expires_at: datetime) -> Authentication:
        """Validate an enrolled credential anew; no caller claims resolve identity."""
        expiry = _expiry(expires_at)
        result: Authentication | None = None
        async with self._factory() as session, session.begin():
            await self._admit_transaction(session)
            principal_id = await session.scalar(
                select(WardCredential.principal_id).where(WardCredential.secret_digest == credential.digest())
            )
            principal = await self._principal(session, principal_id) if principal_id else None
            row = await session.scalar(
                select(WardCredential).where(
                    WardCredential.secret_digest == credential.digest(),
                    WardCredential.principal_id == principal_id,
                )
            )
            now = await self._now(session)
            if (
                audience == AUDIENCE
                and principal is not None
                and row is not None
                and self._credential_current(row, principal, now)
                and row.exchanged_at is None
                and now < expiry <= row.expires_at
            ):
                row.exchanged_at = now
                secret = Secret.generate()
                authentication = WardSession(
                    id=uuid4(),
                    principal_id=principal.id,
                    credential_id=row.id,
                    secret_digest=secret.digest(),
                    audience=AUDIENCE,
                    generation=1,
                    principal_generation=principal.generation,
                    credential_generation=row.generation,
                    expires_at=expiry,
                )
                session.add(authentication)
                await session.flush()
                result = Authentication(authentication.id, principal.id, secret)
                _audit(
                    session,
                    "authenticate",
                    permitted=True,
                    principal=principal,
                    credential=row,
                    authentication=authentication,
                )
            else:
                _audit(session, "authenticate", permitted=False, principal=principal, credential=row)
        if result is None:
            raise _refused()
        return result

    async def assign(self, principal_id: UUID, operation: Operation, *, expires_at: datetime, allowed: bool) -> int:
        """Host-only exact assignment; every mutation advances policy generation."""
        expiry = _expiry(expires_at)
        if type(allowed) is not bool:
            message = "Ward permission requires an explicit boolean."
            raise ValueError(message)
        async with self._factory() as session, session.begin():
            await self._admit_transaction(session)
            principal = await self._principal(session, principal_id)
            if principal is None or principal.revoked_at is not None or expiry <= await self._now(session):
                raise _refused()
            row = await session.scalar(
                select(WardPermission).where(
                    WardPermission.principal_id == principal_id,
                    WardPermission.action == operation.action.value,
                    WardPermission.object_id == operation.object_id,
                    WardPermission.audience == operation.audience,
                    WardPermission.purpose == operation.purpose,
                )
            )
            if row is None:
                row = WardPermission(
                    principal_id=principal_id,
                    action=operation.action.value,
                    object_id=operation.object_id,
                    audience=operation.audience,
                    purpose=operation.purpose,
                )
                session.add(row)
            row.allowed, row.expires_at = allowed, expiry
            principal.policy_generation += 1
            _audit(session, "assign", permitted=allowed, principal=principal, operation=operation)
            return principal.policy_generation

    async def revoke(self, principal_id: UUID, *, credential_id: UUID | None, session_id: UUID | None) -> None:
        """Monotonic terminal revocation; omitted child IDs revoke the Principal."""
        if credential_id is not None and session_id is not None:
            message = "Ward revocation selects exactly one identity."
            raise ValueError(message)
        async with self._factory() as session, session.begin():
            await self._admit_transaction(session)
            principal = await self._principal(session, principal_id)
            if principal is None:
                raise _refused()
            credential = None
            authentication = None
            if credential_id is not None:
                credential = await session.scalar(
                    select(WardCredential).where(
                        WardCredential.id == credential_id, WardCredential.principal_id == principal_id
                    )
                )
                target = credential
            elif session_id is not None:
                authentication = await session.scalar(
                    select(WardSession).where(WardSession.id == session_id, WardSession.principal_id == principal_id)
                )
                target = authentication
            else:
                target = principal
            if target is None:
                raise _refused()
            if target.revoked_at is None:
                target.revoked_at = await self._now(session)
                target.generation += 1
            _audit(
                session,
                "revoke",
                permitted=True,
                principal=principal,
                credential=credential,
                authentication=authentication,
            )

    def _decision_steps(
        self,
        session_secret: Secret,
        operation: Operation,
        expected_policy_generation: int,
    ) -> Generator[Any, Any, tuple[WardPrincipal | None, WardCredential | None, WardSession | None, bool]]:
        """One Ward-owned query sequence/predicate for both transaction interfaces."""
        principal_id = yield select(WardSession.principal_id).where(
            WardSession.secret_digest == session_secret.digest()
        )
        principal = (
            (
                yield select(WardPrincipal)
                .where(WardPrincipal.id == principal_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if principal_id
            else None
        )
        authentication = (
            yield select(WardSession)
            .where(
                WardSession.secret_digest == session_secret.digest(),
                WardSession.principal_id == principal_id,
            )
            .execution_options(populate_existing=True)
        )
        credential = (
            (
                yield select(WardCredential)
                .where(
                    WardCredential.id == authentication.credential_id,
                    WardCredential.principal_id == principal_id,
                )
                .execution_options(populate_existing=True)
            )
            if authentication is not None
            else None
        )
        permission = (
            yield select(WardPermission)
            .where(
                WardPermission.principal_id == principal_id,
                WardPermission.action == operation.action.value,
                WardPermission.object_id == operation.object_id,
                WardPermission.audience == operation.audience,
                WardPermission.purpose == operation.purpose,
                WardPermission.allowed.is_(True),
            )
            .execution_options(populate_existing=True)
        )
        now = cast("datetime", (yield select(func.clock_timestamp())))
        current = (
            principal is not None
            and credential is not None
            and authentication is not None
            and permission is not None
            and self._credential_current(credential, principal, now)
            and authentication.revoked_at is None
            and authentication.audience == operation.audience == AUDIENCE
            and authentication.expires_at > now
            and permission.expires_at > now
            and authentication.principal_generation == principal.generation
            and authentication.credential_generation == credential.generation
            and expected_policy_generation == principal.policy_generation
        )
        return principal, credential, authentication, current

    def _decision_sync(
        self,
        session: Session,
        session_secret: Secret,
        operation: Operation,
        expected_policy_generation: int,
    ) -> tuple[WardPrincipal | None, WardCredential | None, WardSession | None, bool]:
        steps = self._decision_steps(session_secret, operation, expected_policy_generation)
        try:
            statement = next(steps)
            while True:
                statement = steps.send(session.scalar(statement))
        except StopIteration as done:
            return cast("tuple[WardPrincipal | None, WardCredential | None, WardSession | None, bool]", done.value)
        finally:
            steps.close()

    async def _decision(
        self,
        session: AsyncSession,
        session_secret: Secret,
        operation: Operation,
        expected_policy_generation: int,
    ) -> tuple[WardPrincipal | None, WardCredential | None, WardSession | None, bool]:
        steps = self._decision_steps(session_secret, operation, expected_policy_generation)
        try:
            statement = next(steps)
            while True:
                statement = steps.send(await session.scalar(statement))
        except StopIteration as done:
            return cast("tuple[WardPrincipal | None, WardCredential | None, WardSession | None, bool]", done.value)
        finally:
            steps.close()

    @asynccontextmanager
    async def _candidate_scope(
        self, secret: Secret, operation: Operation, expected_policy_generation: int
    ) -> AsyncGenerator[_CandidateScope]:
        """Private fixed-host seam; physical I/O is never passed to publish's callback."""
        require_generation(expected_policy_generation)
        attempt_id = uuid4()
        scope = None
        try:
            async with self._factory() as audit_session, audit_session.begin():
                await self._admit_candidate_transaction(audit_session)
                principal, credential, authentication, current = await self._decision(
                    audit_session, secret, operation, expected_policy_generation
                )
                _audit(
                    audit_session,
                    "publication-attempt",
                    permitted=current,
                    principal=principal,
                    credential=credential,
                    authentication=authentication,
                    operation=operation,
                    attempt_id=attempt_id,
                    outcome="unknown" if current else "refused",
                    expected_policy_generation=expected_policy_generation,
                    issuer_id=self._issuer_id,
                )
            _require_current(current=current)
            async with self._factory() as session, session.begin():
                await self._admit_candidate_transaction(session)
                scope = _CandidateScope(self, session, secret, operation, expected_policy_generation, attempt_id)
                await session.run_sync(scope.check, operation)
                yield scope
        except BaseException as error:
            error.add_note(f"Ward candidate attempt {attempt_id}; reconcile retained owner identities before retry.")
            raise

        finally:
            if scope is not None:
                scope.active = False

    async def publish[T](
        self,
        session_secret: Secret,
        operation: Operation,
        prepared: T,
        publication: Callable[[T, Attribution], None],
        *,
        expected_policy_generation: int,
    ) -> None:
        """Retain an unknown attempt before handoff, then resolve fresh authority.

        An attempt is attribution, never a permit or retry identity. Even a returning
        callback supplies no delivery oracle, so eligible attempts remain unknown.
        """
        require_generation(expected_policy_generation)
        require_synchronous(publication)
        attempt_id = uuid4()
        callback_failure: BaseException | None = None
        try:
            async with self._factory() as session, session.begin():
                await self._admit_transaction(session)
                principal, credential, authentication, current = await self._decision(
                    session,
                    session_secret,
                    operation,
                    expected_policy_generation,
                )
                _audit(
                    session,
                    "publication-attempt",
                    permitted=current,
                    principal=principal,
                    credential=credential,
                    authentication=authentication,
                    operation=operation,
                    attempt_id=attempt_id,
                    outcome="unknown" if current else "refused",
                    expected_policy_generation=expected_policy_generation,
                    issuer_id=self._issuer_id,
                )
            _require_current(current=current)
            # The first transaction has ended before reacquiring the Principal lock.
            async with self._factory() as session, session.begin():
                await self._admit_transaction(session)
                principal, credential, authentication, current = await self._decision(
                    session,
                    session_secret,
                    operation,
                    expected_policy_generation,
                )
                if not current:
                    await session.execute(
                        update(WardAudit).where(WardAudit.id == attempt_id).values(outcome="refused", permitted=False)
                    )
                elif principal is not None and credential is not None and authentication is not None:
                    try:
                        handoff(
                            publication,
                            prepared,
                            Attribution(
                                principal.id,
                                credential.id,
                                authentication.id,
                                principal.generation,
                                credential.generation,
                                authentication.generation,
                                principal.policy_generation,
                                attempt_id,
                            ),
                        )
                    except BaseException as error:
                        callback_failure = error
                        raise
            _require_current(current=current)
        except BaseException as error:
            failure = callback_failure if callback_failure is not None else error
            failure.add_note(
                f"Ward publication attempt {attempt_id}; outcome may be unknown and disclosure may have occurred. "
                "The retained attempt supplies no authority to retry."
            )
            if failure is not error:
                raise failure from error
            raise


class _CandidateScope:
    """Call-confined live transaction, never a returned permission or public authorizer."""

    def __init__(
        self,
        store: DbWardStore,
        session: AsyncSession,
        secret: Secret,
        operation: Operation,
        generation: int,
        attempt_id: UUID,
    ) -> None:
        self.store, self.session, self.secret = store, session, secret
        self.operation, self.generation, self.attempt_id = operation, generation, attempt_id
        self.active, self.thread = True, get_ident()

    def check(self, session: Session, operation: Operation) -> Attribution:
        """Resolve the exact current authority and database time at this owner gate."""
        if (
            not self.active
            or get_ident() != self.thread
            or session is not self.session.sync_session
            or operation.object_id != self.operation.object_id
        ):
            raise _refused()
        principal, credential, authentication, current = self.store._decision_sync(  # noqa: SLF001 - closed host owner seam  # pyright: ignore[reportPrivateUsage]
            session, self.secret, operation, self.generation
        )
        _require_current(current=current)
        if principal is None or credential is None or authentication is None:
            raise _refused()
        return Attribution(
            principal.id,
            credential.id,
            authentication.id,
            principal.generation,
            credential.generation,
            authentication.generation,
            principal.policy_generation,
            self.attempt_id,
        )
