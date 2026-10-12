"""Exact delegated-runtime selection retained with one admitted Run.

This declaration selects an adapter; it carries no execution, privacy or host authority.
"""

from pydantic import BaseModel, ConfigDict, Field

from lychd.domain.delegation.models import DelegatedAgentProfile


class DelegatedRuntimeBinding(BaseModel):
    """Immutable runtime identity and containment posture, without fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    profile: DelegatedAgentProfile = DelegatedAgentProfile.READ
