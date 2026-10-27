"""Authenticated, stateless CPU review of caller-supplied Canvas records."""

from __future__ import annotations

from litestar import Controller, get, post
from litestar.exceptions import ValidationException
from litestar.openapi.datastructures import ResponseSpec
from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST

from lychd.compositions.canvas.operations import review_proposal, validate_document
from lychd.compositions.canvas.schemas import (
    MAX_DOCUMENT_BYTES,
    MAX_REVIEW_BYTES,
    CanvasContracts,
    CanvasDocument,
    CanvasReviewRequest,
    CanvasReviewResult,
)
from lychd.domain.codex.guards import requires_scopes
from lychd.domain.web.contracts import FrameworkError


class CanvasController(Controller):
    """Validate local files and review complete proposals without retaining them."""

    path = "/api/v1/canvas"

    @get(
        "/contracts",
        name="canvas:contracts",
        operation_id="getCanvasContracts",
        guards=[requires_scopes("altar:read")],
    )
    async def contracts(self) -> CanvasContracts:
        """Describe the portable profile and its unavailable external effects."""
        return CanvasContracts()

    @post(
        "/documents/validate",
        name="canvas:validate-document",
        operation_id="validateCanvasDocument",
        status_code=HTTP_200_OK,
        request_max_body_size=MAX_DOCUMENT_BYTES,
        guards=[requires_scopes("altar:read", "canvas:write")],
        responses={HTTP_400_BAD_REQUEST: ResponseSpec(FrameworkError, generate_examples=False)},
    )
    async def document(self, data: CanvasDocument) -> CanvasDocument:
        """Return an identity-validated local document; no file or state is saved."""
        try:
            return validate_document(data)
        except ValueError as exc:
            raise ValidationException(detail="Canvas document validation failed.") from exc

    @post(
        "/review",
        name="canvas:review",
        operation_id="reviewCanvasProposal",
        status_code=HTTP_200_OK,
        request_max_body_size=MAX_REVIEW_BYTES,
        guards=[requires_scopes("altar:read", "canvas:write")],
        responses={HTTP_400_BAD_REQUEST: ResponseSpec(FrameworkError, generate_examples=False)},
    )
    async def review(self, data: CanvasReviewRequest) -> CanvasReviewResult:
        """Return a validated successor of this packet, without applying server state."""
        try:
            return review_proposal(data)
        except ValueError as exc:
            raise ValidationException(detail="Canvas proposal validation failed.") from exc
