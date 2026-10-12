"""Conservative influence labels; neither labels nor joins authorize disclosure."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lychd.domain.privacy.errors import PrivacyRefused

MAX_LABEL_REFERENCES = 128


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    RESTRICTED = "restricted"


class PrivacyCategory(StrEnum):
    DIRECT_IDENTIFIER = "direct-identifier"
    QUASI_IDENTIFIER = "quasi-identifier"
    PRIVATE_DETAIL = "private-detail"
    SECRET = "secret"  # noqa: S105 - category name, never a credential


class HandlingConstraint(StrEnum):
    LOCAL_ONLY = "local-only"
    NO_TRAINING = "no-training"
    NO_RETENTION = "no-retention"


class PrivacyValue(BaseModel):
    """Immutable closed local values; validation errors omit protected inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PrivatizationLabel(PrivacyValue):
    """Influence ancestry is never lowered by sanitizing a derived projection.

    Missing lineage defaults to restricted. Subjects and parents are opaque local
    references, not names. This value is restricted evidence and is not a wire model.
    """

    classification: PrivacyClass = PrivacyClass.RESTRICTED
    weight: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] = 1.0
    categories: frozenset[PrivacyCategory] = frozenset()
    subjects: Annotated[frozenset[UUID], Field(max_length=MAX_LABEL_REFERENCES)] = frozenset()
    material_parents: Annotated[frozenset[UUID], Field(max_length=MAX_LABEL_REFERENCES)] = frozenset()
    constraints: frozenset[HandlingConstraint] = frozenset()
    lineage_known: bool = False

    @model_validator(mode="after")
    def bounded_references(self) -> PrivatizationLabel:
        """Keep even a single local label finite without discarding ancestry."""
        if len(self.subjects) + len(self.material_parents) > MAX_LABEL_REFERENCES:
            msg = "label-reference-bound"
            raise PrivacyRefused(msg)
        return self


def join_labels(labels: tuple[PrivatizationLabel, ...]) -> PrivatizationLabel:
    """Join a complete contributor set, treating absent/unknown lineage as restricted."""
    if not labels:
        return PrivatizationLabel()
    if any(len(label.subjects) + len(label.material_parents) > MAX_LABEL_REFERENCES for label in labels):
        msg = "label-reference-bound"
        raise PrivacyRefused(msg)
    subjects = frozenset(subject for label in labels for subject in label.subjects)
    parents = frozenset(parent for label in labels for parent in label.material_parents)
    if len(subjects) + len(parents) > MAX_LABEL_REFERENCES:
        msg = "label-reference-bound"
        raise PrivacyRefused(msg)
    rank = tuple(PrivacyClass)
    known = all(label.lineage_known for label in labels)
    return PrivatizationLabel(
        classification=max((label.classification for label in labels), key=rank.index)
        if known
        else PrivacyClass.RESTRICTED,
        weight=max(label.weight for label in labels) if known else 1.0,
        categories=frozenset(category for label in labels for category in label.categories),
        subjects=subjects,
        material_parents=parents,
        constraints=frozenset(constraint for label in labels for constraint in label.constraints),
        lineage_known=known,
    )
