"""Generate the Companion's selected Kotlin wire shapes from Altar's exported schema.

This deliberately small generator supports only the shapes used by this Bridge slice. It
fails on unsupported schema changes rather than silently widening a type. Transport semantic
validation and authorization remain separate. Generated files belong in the build directory.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Never

MODELS = (
    "FrameworkError",
    "BridgeTurnView",
    "SessionSummary",
    "SessionView",
    "SessionPage",
    "SessionCreated",
    "RunProjectionSnapshot",
    "BridgeSnapshot",
    "MessageIntent",
    "MessageAccepted",
    "RunEventEnvelope",
)
PACKAGE = "io.github.hexanomicon.lychd.companion.wire"
PRIMITIVE_TYPES = {"string": "String", "integer": "Long", "number": "Double", "boolean": "Boolean"}


class WireSchemaError(ValueError):
    """The selected schema contains a shape this bounded generator cannot preserve."""


def reject_schema(message: str) -> Never:
    """Keep a useful refusal without repeating exception construction across shape handlers."""
    raise WireSchemaError(message)


def kotlin_name(name: str) -> str:
    """Accept the owned snake_case identifiers; reject ambiguous generated identifiers."""
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name):
        reject_schema(f"Unsupported property identifier: {name}")
    first, *rest = name.split("_")
    return first + "".join(part[0].upper() + part[1:] for part in rest)


class WireGenerator:
    """Render only the selected model closure, preserving owner order and absent/null distinctions."""

    def __init__(self, schemas: dict[str, Any]) -> None:
        """Start from the selected Bridge models and discover their schema-owned references."""
        self.schemas = schemas
        self.needed: set[str] = set(MODELS)

    def wire_type(self, shape: dict[str, Any]) -> tuple[str, bool]:
        if not shape:
            return "JsonElement", False
        if "$ref" in shape:
            if any(key in shape for key in ("type", "oneOf", "anyOf", "allOf", "properties", "items")):
                reject_schema("Unsupported structural siblings of a reference")
            return self.reference_type(shape["$ref"])
        if "oneOf" in shape:
            return self.union_type(shape["oneOf"])
        if any(key in shape for key in ("anyOf", "allOf")):
            reject_schema("Unsupported composed schema")
        kind = shape.get("type")
        if kind in PRIMITIVE_TYPES:
            return PRIMITIVE_TYPES[kind], False
        if kind == "array":
            return self.array_type(shape)
        if kind == "object" and shape.get("additionalProperties") == {} and not shape.get("properties"):
            return "JsonObject", False
        reject_schema(f"Unsupported field shape: {shape}")

    def reference_type(self, reference: str) -> tuple[str, bool]:
        prefix = "#/components/schemas/"
        if not reference.startswith(prefix) or not re.fullmatch(r"[A-Z][A-Za-z0-9]*", reference[len(prefix) :]):
            reject_schema(f"Unsupported reference: {reference}")
        name = reference[len(prefix) :]
        if name not in self.schemas:
            reject_schema(f"Missing referenced schema: {name}")
        self.needed.add(name)
        return name, False

    def union_type(self, variants: list[dict[str, Any]]) -> tuple[str, bool]:
        nullable = any(variant.get("type") == "null" for variant in variants)
        values = [variant for variant in variants if variant.get("type") != "null"]
        if len(values) == 1:
            value, nested_null = self.wire_type(values[0])
            return value, nullable or nested_null
        # FrameworkError.extra is explicitly object | array | null in the owner.
        if nullable and {value.get("type") for value in values} == {"object", "array"}:
            for value in values:
                self.wire_type(value)
            return "JsonElement", True
        reject_schema("Unsupported union: review the wire contract before extending generation")

    def array_type(self, shape: dict[str, Any]) -> tuple[str, bool]:
        value, nullable = self.wire_type(shape["items"])
        return f"List<{value}{'?' if nullable else ''}>", False

    def render_model(self, name: str) -> str:
        schema = self.schemas[name]
        if schema.get("type") != "object" or "properties" not in schema:
            reject_schema(f"Expected object model: {name}")
        if any(key in schema for key in ("oneOf", "anyOf", "allOf")) or schema.get("additionalProperties") not in (
            None,
            False,
        ):
            reject_schema(f"Unsupported open or composed model: {name}")
        required = set(schema.get("required", []))
        properties = schema["properties"]
        if not required <= properties.keys():
            reject_schema(f"Required property missing from {name}")
        fields: list[str] = []
        names: set[str] = set()
        for prop, shape in properties.items():
            identifier = kotlin_name(prop)
            if identifier in names:
                reject_schema(f"Property collision in {name}: {prop}")
            names.add(identifier)
            fields.append(self.render_field(prop, identifier, shape, optional=prop not in required))
        return "@Serializable\ndata class " + name + "(\n" + "\n".join(fields) + "\n)\n"

    def render_field(self, prop: str, identifier: str, shape: dict[str, Any], *, optional: bool) -> str:
        value_type, nullable = self.wire_type(shape)
        has_default = "default" in shape and optional
        # Optional without a schema default is absent/null locally; never invent a value.
        suffix = "?" if nullable or (optional and not has_default) else ""
        default = ""
        if has_default:
            if shape["default"] is None and not nullable:
                reject_schema(f"Unsupported null default for nonnullable field: {prop}")
            default = " = " + default_literal(shape["default"], value_type)
        elif optional:
            default = " = null"
        return f'    @SerialName("{prop}") val `{identifier}`: {value_type}{suffix}{default},'

    def render(self) -> str:
        rendered: dict[str, str] = {}
        while remaining := self.needed - rendered.keys():
            name = min(remaining)
            rendered[name] = self.render_model(name)
        models = "\n".join(rendered[name] for name in sorted(rendered))
        shapes = {name: json_shape(self.schemas[name]) for name in sorted(rendered)}
        return models + render_shape_validator(shapes)


def json_shape(shape: dict[str, Any]) -> dict[str, Any]:
    """Retain only owned JSON shape constraints; semantic/admission rules stay in transport."""
    result: dict[str, Any] = {}
    for key in ("$ref", "type", "enum", "const", "required", "additionalProperties"):
        if key in shape:
            result[key] = shape[key]
    if "properties" in shape:
        result["properties"] = {name: json_shape(value) for name, value in shape["properties"].items()}
    if "items" in shape:
        result["items"] = json_shape(shape["items"])
    if "oneOf" in shape:
        result["oneOf"] = [json_shape(value) for value in shape["oneOf"]]
    return result


def render_shape_validator(shapes: dict[str, dict[str, Any]]) -> str:
    """Emit a token-shape oracle from the same selected schema as the generated DTOs."""
    literal = default_literal(json.dumps(shapes, sort_keys=True, separators=(",", ":")), "String")
    return (
        f"\nprivate val wireJsonShapes = Json.parseToJsonElement({literal}).jsonObject\n"
        r"""

/** Validate wire token shape before serializers can widen quoted numbers or absent/null fields. */
fun requireWireJsonShape(model: String, value: JsonElement) {
    val shape = wireJsonShapes[model] as? JsonObject
    require(shape != null && matchesWireJsonShape(shape, value)) { "Invalid wire JSON shape." }
}

private fun matchesWireJsonShape(shape: JsonObject, value: JsonElement): Boolean {
    if (shape.isEmpty()) return true
    shape["\$ref"]?.let { reference ->
        val name = reference.jsonPrimitive.content.substringAfterLast('/')
        val target = wireJsonShapes[name] as? JsonObject ?: return false
        return matchesWireJsonShape(target, value)
    }
    (shape["oneOf"] as? JsonArray)?.let { variants ->
        return variants.count { matchesWireJsonShape(it.jsonObject, value) } == 1
    }
    val primitive = value as? JsonPrimitive
    val valid = when (shape["type"]?.jsonPrimitive?.content) {
        "null" -> value is JsonNull
        "string" -> primitive != null && primitive.isString
        "integer" -> primitive != null && !primitive.isString && primitive.longOrNull != null
        "number" -> primitive != null && !primitive.isString && primitive.doubleOrNull?.isFinite() == true
        "boolean" -> primitive != null && !primitive.isString && primitive.booleanOrNull != null
        "array" -> value is JsonArray && value.all { matchesWireJsonShape(shape.getValue("items").jsonObject, it) }
        "object" -> {
            if (value !is JsonObject) false else {
                val properties = (shape["properties"] as? JsonObject).orEmpty()
                val required = (shape["required"] as? JsonArray).orEmpty().map { it.jsonPrimitive.content }
                required.all(value::containsKey) &&
                    (shape["additionalProperties"] != JsonPrimitive(false) || value.keys.all(properties::containsKey)) &&
                    properties.all { (name, field) -> !value.containsKey(name) || matchesWireJsonShape(field.jsonObject, value.getValue(name)) }
            }
        }
        else -> false
    }
    val allowed = shape["enum"] as? JsonArray
    return valid && (allowed == null || value in allowed) && (!shape.containsKey("const") || value == shape["const"])
}
"""
    )


def default_literal(value: Any, value_type: str) -> str:
    if value is None:
        return "null"
    if value_type == "String" and isinstance(value, str):
        escaped = (
            "\\$"
            if character == "$"
            else "\\u000c"
            if character == "\f"
            else json.dumps(character, ensure_ascii=True)[1:-1]
            for character in value
        )
        return '"' + "".join(escaped) + '"'
    if value_type == "Boolean" and isinstance(value, bool):
        return str(value).lower()
    if value_type == "Long" and isinstance(value, int) and not isinstance(value, bool):
        return f"{value}L"
    if value_type == "Double" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(float(value))
    if value == [] and value_type.startswith("List<"):
        return "emptyList()"
    reject_schema(f"Unsupported default for {value_type}")


def render_models(document: dict[str, Any], digest: str) -> str:
    """Derive selected models and their reference closure without changing missing/null semantics."""
    if document.get("openapi") != "3.1.0":
        reject_schema("Expected the owned OpenAPI 3.1.0 export")
    generator = WireGenerator(document["components"]["schemas"])
    header = (
        "// Generated by tools/generate_wire.py; do not edit.\n"
        f"// Source: clients/web/openapi.json SHA-256 {digest}\n"
        f"package {PACKAGE}\n\n"
        "import kotlinx.serialization.SerialName\n"
        "import kotlinx.serialization.Serializable\n"
        "import kotlinx.serialization.json.Json\n"
        "import kotlinx.serialization.json.JsonArray\n"
        "import kotlinx.serialization.json.JsonElement\n"
        "import kotlinx.serialization.json.JsonNull\n"
        "import kotlinx.serialization.json.JsonObject\n"
        "import kotlinx.serialization.json.JsonPrimitive\n"
        "import kotlinx.serialization.json.booleanOrNull\n"
        "import kotlinx.serialization.json.doubleOrNull\n"
        "import kotlinx.serialization.json.jsonObject\n"
        "import kotlinx.serialization.json.jsonPrimitive\n"
        "import kotlinx.serialization.json.longOrNull\n\n"
    )
    return header + generator.render()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raw = args.schema.read_bytes()
    rendered = render_models(json.loads(raw), hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
