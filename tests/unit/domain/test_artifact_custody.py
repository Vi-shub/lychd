"""Domain evidence validates shape and identity without supplying authority."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from lychd.domain.artifact_custody import ImportSpec, Subject, artifact_id, canonical_bytes


def test_canonical_identifier_and_bounded_claims() -> None:
    store, request = uuid4(), uuid4()
    assert artifact_id(store, request) == f"urn:lychd:artifact:{store}:{request}"
    spec = ImportSpec(request_id=request, owner=Subject(identity="owner"))
    assert ImportSpec.model_validate_json(canonical_bytes(spec)) == spec
    with pytest.raises(ValidationError):
        ImportSpec(request_id=request, owner=Subject(identity="owner"), source_claim="x" * 257)
    with pytest.raises(ValidationError):
        ImportSpec.model_validate({"request_id": request, "owner": spec.owner, "media_type": "image/png"})
    with pytest.raises(ValidationError):
        spec.classification = "public"
