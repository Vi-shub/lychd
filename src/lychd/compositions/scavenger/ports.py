"""Reference-only worker passage to Scavenger's retained application custody."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from lychd.compositions.scavenger.schemas import Digest, Nonnegative, Record

if TYPE_CHECKING:
    from lychd.domain.scout.feed import ScoutFeedBinding, ScoutFeedMaterial
    from lychd.domain.scout.profile import ScoutReviewRef


class ObservationCommit(Record):
    """A confirmed whole-field commit reference, without retrieval authority or payload."""

    admission_id: UUID
    observation_id: UUID
    run_id: UUID
    observation_digest: Digest
    field_digest: Digest
    item_count: Nonnegative


class ScavengerWorkflowPort(Protocol):
    """Check receiving custody on every entry; resolve one commit without acquisition.

    Implementations validate the exact retained Run/seed/consumer and current
    application authority after waits. Adoption rechecks current Scout CONSUME
    and the Run fence inside the locked receiving transaction before commit.
    An uncertain commit resolves only the same immutable occurrence, never a fetch.
    """

    async def committed(self, *, run_id: str, binding: ScoutFeedBinding) -> ObservationCommit | None: ...

    async def adopt(
        self,
        *,
        run_id: str,
        binding: ScoutFeedBinding,
        review: ScoutReviewRef,
        material: ScoutFeedMaterial,
    ) -> ObservationCommit: ...
