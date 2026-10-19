"""Immutable admission values for the explicitly selected semantic source-answer casting."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lychd.domain.archive.contracts import MAX_RECALL_CHARS, MAX_RESULTS
from lychd.domain.archive.derivation import GenerationManifest, GenerationRef
from lychd.domain.archive.semantic import Cosine, SemanticRecallRequest


class SemanticAnswerSelection(BaseModel):
    """Explicit finite semantic policy; no runtime identity or implicit threshold."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    min_cosine: Cosine
    max_results: Annotated[int, Field(strict=True, ge=1, le=MAX_RESULTS)] = 5
    char_budget: Annotated[int, Field(strict=True, ge=1, le=MAX_RECALL_CHARS)] = 8_000

    @field_validator("max_results", "char_budget", mode="before")
    @classmethod
    def integer_budget(cls, value: object) -> int:
        return SemanticRecallRequest.integer_budget(value)

    @field_validator("min_cosine", mode="before")
    @classmethod
    def numeric_threshold(cls, value: object) -> float | int:
        return SemanticRecallRequest.numeric_threshold(value)


class SemanticAnswerBinding(SemanticAnswerSelection):
    """Server-selected generation and exact policy retained in the canonical Run."""

    manifest: GenerationManifest
    generation: GenerationRef

    @model_validator(mode="after")
    def exact_generation(self) -> SemanticAnswerBinding:
        if self.generation.manifest_digest != self.manifest.digest:
            msg = "Semantic answer generation must bind the exact configured manifest."
            raise ValueError(msg)
        return self
