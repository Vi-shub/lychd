"""Check missing/null fidelity and refusal of schema changes the generator cannot represent."""

# Gradle executes these stdlib tests with python3, without requiring pytest or rewritten asserts.
# ruff: noqa: PT009, PT027

import copy
import json
import unittest
from pathlib import Path

from generate_wire import default_literal, json_shape, render_models


class WireGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads((Path(__file__).parents[2] / "web/openapi.json").read_text())

    def test_required_null_is_not_an_optional_default(self) -> None:
        text = render_models(self.document, "test")
        self.assertIn("val `capabilityKey`: String?,", text)
        self.assertNotIn("val `capabilityKey`: String? = null", text)
        self.assertIn("val `schemaVersion`: Long = 1L,", text)
        self.assertIn("val `runId`: String? = null,", text)
        self.assertIn("val `fragments`: List<JsonObject>? = null,", text)
        self.assertIn("val `prompt`: String,", text)

    def test_reference_closure_is_deterministic_and_includes_owned_consent_shape(self) -> None:
        first = render_models(self.document, "test")
        second = render_models(copy.deepcopy(self.document), "test")
        self.assertEqual(first, second)
        self.assertIn("data class ConsentCard(", first)
        self.assertIn("val `pendingConsents`: List<ConsentCard>,", first)
        self.assertIn("val `extra`: JsonElement? = null,", first)

    def test_new_unsupported_union_is_refused(self) -> None:
        self.document["components"]["schemas"]["MessageIntent"]["properties"]["prompt"] = {
            "oneOf": [{"type": "string"}, {"type": "integer"}]
        }
        with self.assertRaisesRegex(ValueError, "Unsupported union"):
            render_models(self.document, "test")

    def test_missing_reference_is_refused(self) -> None:
        del self.document["components"]["schemas"]["ConsentCard"]
        with self.assertRaisesRegex(ValueError, "Missing referenced schema"):
            render_models(self.document, "test")

    def test_missing_required_property_is_refused(self) -> None:
        self.document["components"]["schemas"]["MessageIntent"]["required"].append("gone")
        with self.assertRaisesRegex(ValueError, "Required property missing"):
            render_models(self.document, "test")

    def test_nonnullable_field_cannot_render_a_null_default(self) -> None:
        self.document["components"]["schemas"]["BridgeTurnView"]["properties"]["state"]["default"] = None
        with self.assertRaisesRegex(ValueError, "Unsupported null default for nonnullable field"):
            render_models(self.document, "test")

    def test_shape_metadata_tracks_owned_required_null_ref_and_closed_object_changes(self) -> None:
        schemas = self.document["components"]["schemas"]
        snapshot = json_shape(schemas["BridgeSnapshot"])
        self.assertIn("session", snapshot["required"])
        self.assertEqual(snapshot["properties"]["session"]["oneOf"][1], {"type": "null"})
        self.assertEqual(snapshot["properties"]["sessions"]["items"], {"$ref": "#/components/schemas/SessionSummary"})
        self.assertFalse(snapshot["additionalProperties"])
        self.assertNotIn("turns", json_shape(schemas["SessionView"])["required"])
        self.assertEqual(json_shape(schemas["RunEventEnvelope"])["properties"]["schema_version"]["const"], 1)
        schemas["MessageIntent"]["properties"]["reviewed"] = {"type": "boolean"}
        schemas["MessageIntent"]["required"].append("reviewed")
        text = render_models(self.document, "test")
        self.assertIn("val `reviewed`: Boolean,", text)
        self.assertIn('\\"reviewed\\":{\\"type\\":\\"boolean\\"}', text)
        self.assertIn("fun requireWireJsonShape(model: String, value: JsonElement)", text)

    def test_structural_reference_siblings_and_open_dto_models_are_refused(self) -> None:
        schemas = self.document["components"]["schemas"]
        schemas["SessionCreated"]["properties"]["session"]["type"] = "string"
        with self.assertRaisesRegex(ValueError, "structural siblings"):
            render_models(self.document, "test")
        del schemas["SessionCreated"]["properties"]["session"]["type"]
        schemas["MessageIntent"]["additionalProperties"] = True
        with self.assertRaisesRegex(ValueError, "Unsupported open or composed model"):
            render_models(self.document, "test")

    def test_string_defaults_use_kotlin_escapes_for_formfeed_and_interpolation(self) -> None:
        self.assertEqual(default_literal("\f$identity", "String"), '"\\u000c\\$identity"')
        self.assertEqual(default_literal("\\f", "String"), '"\\\\f"')


if __name__ == "__main__":
    unittest.main()
