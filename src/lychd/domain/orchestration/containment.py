"""Write-ahead evidence for runtime-native activation without a model inverse."""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lychd.domain.orchestration.actuator import AnimatorId


class SoftActivationRecord(BaseModel):
    """One activation whose unfinished effect must survive a Vessel restart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    activation_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    target_animator: AnimatorId
    target_capability_key: str = Field(min_length=1)
    config_generation: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SoftActivationGuard(Protocol):
    """Persist before invocation, clear only after verified convergence."""

    def assert_clear(self) -> None:
        """Refuse admission when any unfinished attempt or unsafe storage exists."""
        ...

    def arm(self, record: SoftActivationRecord) -> None:
        """Durably retain this attempt before its activation can be invoked."""
        ...

    def clear(self, record: SoftActivationRecord) -> None:
        """Clear only this exact attempt after authoritative settlement."""
        ...
