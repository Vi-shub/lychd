"""Authenticated first-feed admission from a retained Scavenger request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from lychd.domain.codex.local_access import LocalOperatorAccess, LocalOperatorAuthentication
from lychd.domain.scout.attempts import (
    SCOUT_FEED_PURPOSE,
    SCOUT_FEED_URL,
    LocatorInfluence,
    LocatorSource,
    PublicLocatorDeclaration,
    ScoutFeedConsumer,
)
from lychd.domain.scout.authority import ScoutHostAuthorizer
from lychd.domain.scout.feed import ScoutFeedBinding, ScoutFeedDeclarationProof, ScoutFeedProfile
from lychd.domain.scout.profile import ScoutWorkflowCode, ScoutWorkflowError
from lychd.domain.scout.transport import FetchError

if TYPE_CHECKING:
    from lychd.agents.router import Intent


class ScoutFeedAdmissionService:
    """Bind the actual marker and exact receiving request before held Run publication.

    The application owner first retains the complete seed/filter and supplies their
    exact references and deadline. This service cannot establish that custody from
    a shaped consumer, and neither its declaration nor its proof grants Fetch.
    The selected application assembly must retain/bind its request before release.
    """

    def __init__(
        self, *, access: LocalOperatorAccess, authorizer: ScoutHostAuthorizer, profile: ScoutFeedProfile
    ) -> None:
        """Share the credential issuer and current Scout policy used by acquisition."""
        self._access, self._authorizer, self._profile = access, authorizer, profile

    def _classify(self, declaration: PublicLocatorDeclaration) -> None:
        try:
            self._authorizer.require_declaration(
                declaration,
                binding_digest=self._profile.binding_digest,
                resolver_custody_digest=self._profile.resolver_custody_digest,
                revocation_generation=self._profile.revocation_generation,
            )
        except FetchError:
            raise ScoutWorkflowError(ScoutWorkflowCode.ADMISSION) from None

    def declare(
        self, *, consumer: ScoutFeedConsumer, request_id: UUID, deadline: datetime, authentication: object
    ) -> ScoutFeedDeclarationProof:
        """Assert one fixed locator; never infer source, expiry or request facts."""
        now = datetime.now(UTC)
        if (
            not self._access.current(authentication)
            or type(authentication) is not LocalOperatorAuthentication
            or type(consumer) is not ScoutFeedConsumer
            or type(deadline) is not datetime
            or deadline.utcoffset() is None
            or deadline <= now
        ):
            raise ScoutWorkflowError(ScoutWorkflowCode.ADMISSION)
        declaration = PublicLocatorDeclaration(
            url=SCOUT_FEED_URL,
            subject=authentication.subject,
            authentication_generation=authentication.authentication_generation,
            purpose=SCOUT_FEED_PURPOSE,
            source_manifest=(LocatorSource(source_id=request_id, url=SCOUT_FEED_URL),),
            influence_manifest=(LocatorInfluence(source_id=request_id),),
            policy_revision=self._profile.policy_revision,
            declared_at=now,
        )
        self._classify(declaration)
        return ScoutFeedDeclarationProof(
            authentication=authentication,
            declaration=declaration,
            consumer=consumer,
            request_id=request_id,
            expires_at=min(deadline, now + timedelta(seconds=self._profile.review_ttl_seconds)),
        )

    def __call__(self, intent: Intent, proof: ScoutFeedDeclarationProof, profile: ScoutFeedProfile) -> ScoutFeedBinding:
        """Recheck possession, current policy and exact request after admission waits."""
        if (
            type(proof) is not ScoutFeedDeclarationProof
            or not self._access.current(proof.authentication)
            or type(proof.consumer) is not ScoutFeedConsumer
            or type(proof.expires_at) is not datetime
            or profile != self._profile
            or intent.source != "scavenger"
            or intent.prompt != str(proof.consumer.application_admission_id)
            or proof.expires_at <= datetime.now(UTC)
        ):
            raise ScoutWorkflowError(ScoutWorkflowCode.ADMISSION)
        declaration = proof.declaration
        if (
            declaration.subject != proof.authentication.subject
            or declaration.authentication_generation != proof.authentication.authentication_generation
            or declaration.source_manifest != (LocatorSource(source_id=proof.request_id, url=SCOUT_FEED_URL),)
            or declaration.influence_manifest != (LocatorInfluence(source_id=proof.request_id),)
        ):
            raise ScoutWorkflowError(ScoutWorkflowCode.ADMISSION)
        self._classify(declaration)
        return ScoutFeedBinding(
            profile=profile,
            declaration=declaration,
            consumer=proof.consumer,
            request_id=proof.request_id,
            expires_at=proof.expires_at,
        )
