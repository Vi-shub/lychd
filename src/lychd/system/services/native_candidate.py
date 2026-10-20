"""Isolated fixed candidate consumer: inline blocking custody, no executor or activation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from lychd.compositions.creation.native_candidate import (
    NativeAssessment,
    NativeBinding,
    NativeDecision,
    NativePlan,
    NativeRequest,
    NativeView,
)
from lychd.db.creation import DbCreationStore
from lychd.db.ward import DbWardStore
from lychd.domain.artifact_custody import (
    NATIVE_CANDIDATE_MEDIA_TYPE,
    NATIVE_CANDIDATE_PROFILE,
    Action,
    Admission,
    ArtifactRecord,
    AuthorizationRequest,
    Continuity,
    CustodyCode,
    CustodyRefused,
    Decision,
    ImportSpec,
    RootBinding,
    Subject,
    artifact_id,
    canonical_bytes,
    digest_bytes,
)
from lychd.domain.artifacts import ArtifactRef
from lychd.domain.evolution.native_candidate import admit_native_candidate, decode_native_candidate
from lychd.domain.ward.service import Action as WardAction
from lychd.domain.ward.service import Attribution, Operation, Secret, WardRefusedError, WardService
from lychd.system.services.artifact_custody import LocalArtifactCustody

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from lychd.db.ward import _CandidateScope  # pyright: ignore[reportPrivateUsage]


def _subject(identity: object) -> Subject:
    return Subject(identity=f"ward:{identity}")


class _Gate:
    """Private authorizer is inert except inside one live fixed invocation."""

    def __init__(self, plan: NativePlan, content: bytes) -> None:
        self.plan, self.content = plan, content
        self.binding: RootBinding | None = None
        self.record: ArtifactRecord | None = None
        self.scope: _CandidateScope | None = None
        self.session: Session | None = None

    def authorize(self, request: AuthorizationRequest) -> Decision:
        if self.scope is None or self.session is None or request.binding != self.binding:
            raise CustodyRefused(CustodyCode.DENIED)
        operations = {
            Action.IMPORT: (WardAction.ADMIT, "native:import"),
            Action.INSPECT: (WardAction.INSPECT, "native:inspect"),
            Action.ADMIT: (WardAction.ADMIT, "native:structure"),
            Action.READ: (WardAction.READ, "native:bytes"),
        }
        pair = operations.get(request.action)
        if pair is None:
            raise CustodyRefused(CustodyCode.DENIED)
        try:
            actor = self.scope.check(self.session, Operation(pair[0], self.plan.candidate_id, pair[1]))
        except WardRefusedError:
            raise CustodyRefused(CustodyCode.DENIED) from None
        owner = _subject(self.plan.proposer)
        if (
            request.subject != _subject(actor.principal_id)
            or request.owner != owner
            or request.importer != owner
            or request.purpose != self.scope.operation.purpose
            or request.reference.digest != self.plan.bundle_digest
            or request.reference.size != len(self.content)
            or request.reference.artifact_id != artifact_id(request.binding.store_id, self.plan.candidate_id)
            or request.reference.media_type != NATIVE_CANDIDATE_MEDIA_TYPE
            or request.reference.classification != "private"
            or (self.record is not None and request.record is not None and request.record != self.record)
        ):
            raise CustodyRefused(CustodyCode.DENIED)
        spec = ImportSpec(request_id=self.plan.candidate_id, owner=owner, media_type=NATIVE_CANDIDATE_MEDIA_TYPE)
        fingerprint = digest_bytes(
            canonical_bytes(spec) + b"\0" + canonical_bytes(owner) + b"\0" + self.plan.bundle_digest.encode()
        )
        if request.import_fingerprint != fingerprint or (request.proposal is not None and request.proposal != spec):
            raise CustodyRefused(CustodyCode.DENIED)
        return Decision(request=request, allowed=True)


class NativeCandidateService:
    """One exact candidate/private store. Public methods accept no foreign executable code."""

    def __init__(self, *, ward: DbWardStore, creation: DbCreationStore, plan: NativePlan, content: bytes) -> None:
        """Validate a closed literal source plan before any custody activation."""
        self.__ward, self.__creation = ward, creation
        self.__plan = NativePlan.model_validate_json(canonical_bytes(plan))
        if type(content) is not bytes or digest_bytes(content) != self.__plan.bundle_digest:
            message = "Native source bytes differ from the known plan."
            raise ValueError(message)
        source = ArtifactRef(
            artifact_id="urn:lychd:native:preparation",
            digest=digest_bytes(content),
            size=len(content),
            media_type=NATIVE_CANDIDATE_MEDIA_TYPE,
        )
        bundle = decode_native_candidate(source, content)
        if (
            bundle.base_commit != plan.base_commit
            or bundle.selected_paths != plan.selected_paths
            or bundle.allowed_paths != plan.allowed_paths
            or digest_bytes(canonical_bytes(bundle.closure)) != plan.closure_digest
        ):
            message = "Native source selection differs from the known plan."
            raise ValueError(message)
        self.__content, self.__gate = content, _Gate(self.__plan, content)
        self.__lock = asyncio.Lock()
        self.__store: LocalArtifactCustody | None = None

    @classmethod
    def create(
        cls, root: Path, *, ward: DbWardStore, creation: DbCreationStore, plan: NativePlan, content: bytes
    ) -> Self:
        service = cls(ward=ward, creation=creation, plan=plan, content=content)
        service.__store = LocalArtifactCustody.create(root, authorizer=service.__gate, profile=NATIVE_CANDIDATE_PROFILE)
        service.__gate.binding = service.__store.binding
        return service

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        ward: DbWardStore,
        creation: DbCreationStore,
        plan: NativePlan,
        content: bytes,
        binding: RootBinding,
        record: ArtifactRecord,
        continuity: Continuity,
    ) -> Self:
        service = cls(ward=ward, creation=creation, plan=plan, content=content)
        if record.binding != binding or binding.profile != NATIVE_CANDIDATE_PROFILE:
            message = "Native reopen binding differs."
            raise ValueError(message)
        service.__plan.inspect(record, content)
        service.__store = LocalArtifactCustody.open(
            root, binding=binding, authorizer=service.__gate, continuity=continuity
        )
        service.__gate.binding, service.__gate.record = binding, record
        return service

    @property
    def binding(self) -> RootBinding:
        """Non-authorizing root continuity evidence for this explicit host owner."""
        if self.__gate.binding is None:
            raise CustodyRefused(CustodyCode.CLOSED)
        return self.__gate.binding

    async def close(self) -> None:
        async with self.__lock:
            if self.__store is not None:
                self.__store.close()
                self.__store = None

    async def __run(
        self,
        secret: Secret,
        generation: int,
        action: WardAction,
        purpose: str,
        work: Callable[[Session, _CandidateScope], Any],
    ) -> Any:
        async with self.__lock:
            if self.__store is None:
                raise CustodyRefused(CustodyCode.CLOSED)
            operation = Operation(action, self.__plan.candidate_id, purpose)
            async with self.__ward._candidate_scope(secret, operation, generation) as scope:  # noqa: SLF001 - closed host owner seam  # pyright: ignore[reportPrivateUsage]

                def fixed(session: Session) -> Any:
                    actor = scope.check(session, operation)
                    if dict(self.__plan.policy_generations).get(actor.principal_id) != generation:
                        raise CustodyRefused(CustodyCode.DENIED)
                    self.__gate.scope, self.__gate.session = scope, session
                    try:
                        return work(session, scope)
                    finally:
                        self.__gate.scope = self.__gate.session = None

                return await scope.session.run_sync(fixed)

    def __record(self) -> ArtifactRecord:
        if self.__gate.record is None:
            raise CustodyRefused(CustodyCode.REFERENCE)
        return self.__gate.record

    def __read(self, scope: _CandidateScope) -> bytes:
        if self.__store is None:
            raise CustodyRefused(CustodyCode.CLOSED)
        actor = scope.check(scope.session.sync_session, scope.operation)
        content = self.__store.read(
            self.__record(), subject=_subject(actor.principal_id), purpose=scope.operation.purpose
        )
        self.__plan.inspect(self.__record(), content)
        return content

    async def import_candidate(self, secret: Secret, *, expected_policy_generation: int) -> None:
        def fixed(session: Session, scope: _CandidateScope) -> None:
            actor = scope.check(session, scope.operation)
            if actor.principal_id != self.__plan.proposer:
                raise CustodyRefused(CustodyCode.DENIED)
            if self.__store is None:
                raise CustodyRefused(CustodyCode.CLOSED)
            owner = _subject(actor.principal_id)
            spec = ImportSpec(request_id=self.__plan.candidate_id, owner=owner, media_type=NATIVE_CANDIDATE_MEDIA_TYPE)
            self.__gate.record = self.__store.import_bytes(
                self.__content, spec, subject=owner, purpose=scope.operation.purpose
            ).record

        await self.__run(secret, expected_policy_generation, WardAction.ADMIT, "native:import", fixed)

    def __structure(self, session: Session, scope: _CandidateScope) -> Admission:
        actor = scope.check(session, scope.operation)
        if self.__store is None:
            raise CustodyRefused(CustodyCode.CLOSED)
        return admit_native_candidate(
            self.__store, self.__record(), subject=_subject(actor.principal_id), purpose=scope.operation.purpose
        )

    async def admit_structure(self, secret: Secret, *, expected_policy_generation: int) -> None:
        await self.__run(secret, expected_policy_generation, WardAction.ADMIT, "native:structure", self.__structure)

    async def bind_inactive_case(
        self, secret: Secret, request: NativeRequest, *, expected_policy_generation: int
    ) -> None:
        request = NativeRequest.model_validate_json(canonical_bytes(request))
        if request.plan != self.__plan:
            message = "Native preparation changed."
            raise ValueError(message)

        def fixed(session: Session, scope: _CandidateScope) -> None:
            admission = self.__structure(session, scope)
            self.__plan.inspect(self.__record(), self.__read(scope))
            self.__creation._native_bind(  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
                session, scope, NativeBinding(request=request, record=self.__record(), admission=admission)
            )

        await self.__run(secret, expected_policy_generation, WardAction.ADMIT, "native:bind", fixed)

    async def append_assessment(
        self, secret: Secret, request: NativeRequest, body: NativeAssessment, *, expected_policy_generation: int
    ) -> None:
        body = NativeAssessment.model_validate_json(canonical_bytes(body))
        await self.__feedback(secret, request, body, expected_policy_generation, "native:assess")

    async def decide_inactive(
        self, secret: Secret, request: NativeRequest, body: NativeDecision, *, expected_policy_generation: int
    ) -> None:
        body = NativeDecision.model_validate_json(canonical_bytes(body))
        await self.__feedback(secret, request, body, expected_policy_generation, "native:decide")

    async def __feedback(
        self,
        secret: Secret,
        request: NativeRequest,
        body: NativeAssessment | NativeDecision,
        generation: int,
        purpose: str,
    ) -> None:
        request = NativeRequest.model_validate_json(canonical_bytes(request))
        if request.plan != self.__plan:
            message = "Native preparation changed."
            raise ValueError(message)

        def fixed(session: Session, scope: _CandidateScope) -> None:
            self.__read(scope)
            _, binding = self.__creation._native_case(session, request)  # noqa: SLF001 - closed host owner seam  # pyright: ignore[reportPrivateUsage]
            if binding.request != request or binding.record != self.__record():
                message = "Native case differs from retained custody."
                raise ValueError(message)
            self.__creation._native_feedback(session, scope, request, body)  # noqa: SLF001 - closed host owner seam  # pyright: ignore[reportPrivateUsage]

        await self.__run(secret, generation, WardAction.DECIDE, purpose, fixed)

    async def project_case(
        self,
        secret: Secret,
        request: NativeRequest,
        *,
        expected_policy_generation: int,
        publication: Callable[[NativeView, Attribution], None],
    ) -> None:
        """Only the old no-I/O Ward publication callback is public; no raw bytes escape."""

        async def prepare() -> NativeView:
            def fixed(session: Session, scope: _CandidateScope) -> NativeView:
                self.__read(scope)
                view = self.__creation._native_view(session, scope, request)  # noqa: SLF001 - closed host owner seam  # pyright: ignore[reportPrivateUsage]
                if view.binding.request != request or request.plan != self.__plan:
                    message = "Native projection differs."
                    raise ValueError(message)
                return view

            return await self.__run(secret, expected_policy_generation, WardAction.READ, "native:view", fixed)

        await WardService(self.__ward).publish(
            secret,
            Operation(WardAction.READ, self.__plan.candidate_id, "native:view"),
            prepare=prepare,
            publication=publication,
            expected_policy_generation=expected_policy_generation,
        )
