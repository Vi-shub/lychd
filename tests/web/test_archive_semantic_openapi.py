"""The generated browser contract must expose the selected semantic API operations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts import export_openapi


def test_semantic_operations_and_required_threshold_survive_contract_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "openapi.json"
    monkeypatch.setattr(export_openapi, "OUTPUT", output)
    export_openapi.main()
    schema = json.loads(output.read_text())
    expected = {
        "/api/v1/archive/semantic/recalls": "recallArchiveSemanticNotes",
        "/api/v1/archive/semantic/recalls/inspect": "inspectArchiveSemanticRecall",
        "/api/v1/archive/semantic/recalls/{receipt_id}/resolve": "resolveArchiveSemanticRecall",
    }
    assert set(expected) <= set(schema["paths"])
    for path, operation in expected.items():
        assert schema["paths"][path]["post"]["operationId"] == operation
    request = schema["components"]["schemas"]["SemanticRecallRequest"]
    assert "min_cosine" in request["required"]
    assert request["properties"]["max_results"]["type"] == "integer"
    assert request["properties"]["char_budget"]["type"] == "integer"
