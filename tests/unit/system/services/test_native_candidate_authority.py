"""Real local custody with substituted consultation: not credential-backed authority evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from lychd.compositions.creation.native_candidate import NativeBinding, NativePlan, NativeRequest
from lychd.compositions.creation.schemas import CreationRefused, canonical
from lychd.db.creation import DbCreationStore
from lychd.db.models.creation import CreationAdmission, CreationCase, CreationFeedback
from lychd.db.ward import DbWardStore
from lychd.domain.artifact_custody import Continuity, CustodyRefused, canonical_bytes, digest_bytes
from lychd.domain.ward.service import Attribution, Operation, Secret, WardRefusedError
from lychd.system.services import artifact_custody as physical
from lychd.system.services.native_candidate import NativeCandidateService
from tests.unit.system.services.test_native_candidate_custody import bundle


def paths(root: Path) -> set[Path]:
    return set(root.iterdir())


def identities(root: Path) -> dict[str, int]:
    return {entry.name: entry.stat().st_ino for entry in root.iterdir()}


def plan(proposer: UUID, verifier: UUID, generation: int = 1) -> tuple[NativePlan, bytes]:
    value = bundle()
    content = canonical_bytes(value)
    return NativePlan(
        candidate_id=uuid4(),
        proposer=proposer,
        verifier=verifier,
        decision_owner=proposer,
        policy_generations=((proposer, generation), (verifier, generation)),
        bundle_digest=digest_bytes(content),
        base_commit=value.base_commit,
        selected_paths=value.selected_paths,
        allowed_paths=value.allowed_paths,
        closure_digest=digest_bytes(canonical_bytes(value.closure)),
        policy_digest=digest_bytes(b"policy"),
        history_digest=digest_bytes(b"history"),
        recovery_digest=digest_bytes(b"recovery"),
        criteria=("exact-bytes",),
    ), content


class Session:
    def __init__(self) -> None:
        self.sync_session = object()

    async def run_sync(self, work: Callable[..., Any], *arguments: Any) -> Any:
        return work(self.sync_session, *arguments)


class Scope:
    def __init__(self, ward: Ward, operation: Operation, generation: int) -> None:
        self.ward, self.operation, self.generation = ward, operation, generation
        self.session, self.active = Session(), True

    def check(self, session: object, operation: Operation) -> Attribution:
        self.ward.gates.append(operation)
        if (
            not self.active
            or session is not self.session.sync_session
            or self.ward.denied
            or operation.object_id != self.operation.object_id
            or self.generation != self.ward.generation
        ):
            message = "Substituted current gate refused."
            raise WardRefusedError(message)
        return Attribution(self.ward.actor, uuid4(), uuid4(), 1, 1, 1, self.generation, uuid4())


class Ward:
    def __init__(self, actor: UUID) -> None:
        self.actor, self.generation, self.denied = actor, 1, False
        self.gates: list[Operation] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.pause, self.lose_ack = False, False

    @asynccontextmanager
    async def _candidate_scope(self, secret: Secret, operation: Operation, generation: int) -> AsyncGenerator[Scope]:
        del secret
        scope = Scope(self, operation, generation)
        self.entered.set()
        if self.pause:
            await self.release.wait()
        try:
            scope.check(scope.session.sync_session, operation)
            yield scope
            if self.lose_ack:
                self.lose_ack = False
                message = "Substituted acknowledgement loss."
                raise ConnectionError(message)
        finally:
            scope.active = False


class Creation:
    def __init__(self) -> None:
        self.binding: NativeBinding | None = None

    def _native_bind(self, session: object, scope: Scope, binding: NativeBinding) -> None:
        scope.check(session, scope.operation)
        self.binding = binding


def service(root: Path, ward: Ward, creation: Creation, known: NativePlan, content: bytes) -> NativeCandidateService:
    return NativeCandidateService.create(
        root,
        ward=cast("DbWardStore", cast("object", ward)),
        creation=cast("DbCreationStore", cast("object", creation)),
        plan=known,
        content=content,
    )


def request(known: NativePlan) -> NativeRequest:
    return NativeRequest(
        case_id=uuid4(), admission_id=uuid4(), plan=known, deadline=datetime.now(UTC) + timedelta(hours=1)
    )


@pytest.mark.asyncio
async def test_current_gate_refuses_after_real_staging_and_cleans_exact_private_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ward, creation = Ward(uuid4()), Creation()
    known, content = plan(ward.actor, uuid4())
    host = service(tmp_path, ward, creation, known, content)
    original = physical.publication.stage_bytes_file
    before = paths(tmp_path)

    def staged(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        ward.denied = True
        return result

    monkeypatch.setattr(physical.publication, "stage_bytes_file", staged)
    with pytest.raises(CustodyRefused, match="denied"):
        await host.import_candidate(Secret.generate(), expected_policy_generation=1)
    assert paths(tmp_path) == before
    assert len(ward.gates) >= 3
    await host.close()


@pytest.mark.asyncio
async def test_real_custody_exact_retry_lost_ack_and_continuous_reopen(tmp_path: Path) -> None:
    ward, creation = Ward(uuid4()), Creation()
    known, content = plan(ward.actor, uuid4())
    host = service(tmp_path, ward, creation, known, content)
    secret = Secret.generate()
    ward.lose_ack = True
    with pytest.raises(ConnectionError, match="acknowledgement"):
        await host.import_candidate(secret, expected_policy_generation=1)
    before = identities(tmp_path)
    await host.import_candidate(secret, expected_policy_generation=1)
    assert before == identities(tmp_path)
    await host.bind_inactive_case(secret, request(known), expected_policy_generation=1)
    retained = creation.binding
    assert retained is not None
    assert retained.record.reference.digest == known.bundle_digest
    assert {operation.purpose for operation in ward.gates} >= {
        "native:import",
        "native:inspect",
        "native:structure",
        "native:bind",
        "native:bytes",
    }
    binding = host.binding
    await host.close()
    reopened = NativeCandidateService.open(
        tmp_path,
        ward=cast("DbWardStore", cast("object", ward)),
        creation=cast("DbCreationStore", cast("object", creation)),
        plan=known,
        content=content,
        binding=binding,
        record=retained.record,
        continuity=Continuity.CONTINUOUS,
    )
    ward.denied = True
    with pytest.raises(WardRefusedError):
        await reopened.admit_structure(secret, expected_policy_generation=1)
    await reopened.close()


@pytest.mark.asyncio
async def test_serialized_shutdown_and_cancelled_scope_never_leave_live_authorizer(tmp_path: Path) -> None:
    ward, creation = Ward(uuid4()), Creation()
    known, content = plan(ward.actor, uuid4())
    host = service(tmp_path, ward, creation, known, content)
    ward.pause = True
    operation = asyncio.create_task(host.import_candidate(Secret.generate(), expected_policy_generation=1))
    await ward.entered.wait()
    close = asyncio.create_task(host.close())
    await asyncio.sleep(0)
    assert not close.done()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await close
    with pytest.raises(CustodyRefused, match="closed"):
        await host.import_candidate(Secret.generate(), expected_policy_generation=1)
    assert {path.name for path in paths(tmp_path)} == {"custody.lock", "custody.root"}


@pytest.mark.asyncio
async def test_changed_generation_or_candidate_bytes_cannot_rebind_preparation(tmp_path: Path) -> None:
    ward, creation = Ward(uuid4()), Creation()
    known, content = plan(ward.actor, uuid4())
    host = service(tmp_path, ward, creation, known, content)
    ward.generation = 2
    with pytest.raises(WardRefusedError):
        await host.import_candidate(Secret.generate(), expected_policy_generation=1)
    with pytest.raises(CustodyRefused, match="denied"):
        await host.import_candidate(Secret.generate(), expected_policy_generation=2)
    await host.close()
    with pytest.raises(ValueError, match="bytes"):
        service(tmp_path, ward, creation, known, content + b" ")
    with pytest.raises(ValueError, match="independently"):
        NativePlan.model_validate_json(canonical_bytes(known.model_copy(update={"verifier": known.proposer})))


def test_closed_service_accepts_no_inspector_or_effect_callback() -> None:
    import inspect

    for method in (
        NativeCandidateService.import_candidate,
        NativeCandidateService.admit_structure,
        NativeCandidateService.bind_inactive_case,
        NativeCandidateService.decide_inactive,
    ):
        assert "inspector" not in inspect.signature(method).parameters
        assert "publication" not in inspect.signature(method).parameters
    assert not hasattr(NativeCandidateService, "store")
    assert not hasattr(NativeCandidateService, "authorizer")


def test_native_case_query_filters_the_complete_authorized_request_before_loading() -> None:
    statements: list[Any] = []

    class Probe:
        def scalar(self, statement: Any) -> None:
            statements.append(statement)

    known, _content = plan(uuid4(), uuid4())
    target = request(known)
    with pytest.raises(CreationRefused, match="native_case"):
        DbCreationStore._native_case(Probe(), target)  # pyright: ignore[reportPrivateUsage]
    compiled = statements[0].compile()
    assert "creation_case.request_bytes =" in str(compiled)
    assert canonical(target) in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [CreationCase, CreationAdmission, CreationFeedback])
@pytest.mark.parametrize("route", ["mapper", "table", "masked-table"])
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "AUTOCOMMIT"])
async def test_split_creation_bind_refuses_before_sql_or_physical_candidate_effect(
    tmp_path: Path,
    model: Any,
    route: str,
    isolation: str,
) -> None:
    """Real SQLAlchemy routing objects; connection attempts are forbidden before sockets."""
    admitted = create_async_engine("postgresql+asyncpg://")
    alternate = create_async_engine("postgresql+asyncpg://", isolation_level=isolation)
    attempts: list[str] = []

    def no_connection(*_arguments: Any, **_keywords: Any) -> None:
        attempts.append("connection")
        message = "Native bind refusal must precede any database connection."
        raise AssertionError(message)

    event.listen(admitted.sync_engine, "do_connect", no_connection)
    event.listen(alternate.sync_engine, "do_connect", no_connection)
    binds: dict[Any, Any] = {model: alternate} if route == "mapper" else {model.__table__: alternate}
    if route == "masked-table":
        binds = {model: admitted, model.__table__: alternate}
    factory = async_sessionmaker(admitted, binds=binds, expire_on_commit=False)
    known, content = plan(uuid4(), uuid4())
    host = NativeCandidateService.create(
        tmp_path,
        ward=DbWardStore(factory, issuer_id=uuid4()),
        creation=DbCreationStore(factory, policy=None),
        plan=known,
        content=content,
    )
    before = identities(tmp_path)
    try:
        with pytest.raises(WardRefusedError, match="exact AsyncEngine"):
            await host.import_candidate(Secret.generate(), expected_policy_generation=1)
        with pytest.raises(WardRefusedError, match="exact AsyncEngine"):
            await host.bind_inactive_case(Secret.generate(), request(known), expected_policy_generation=1)
        assert identities(tmp_path) == before
        assert not attempts
    finally:
        await host.close()
        await admitted.dispose()
        await alternate.dispose()
