"""Explicit bounded JSON Schema subset; validation never coerces or invents fields."""

import json
import math
import re

from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators

from llm_gateway_core.contracts import GatewayError

KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "anyOf",
    "$defs",
    "$ref",
    "title",
    "description",
    "default",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}

StrictValidator = validators.extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ).redefine("number", lambda checker, value: type(value) in (int, float) and math.isfinite(value)),
)


def check_schema(schema: dict):
    if len(json.dumps(schema)) > 65536:
        raise GatewayError("REQUEST_INVALID", "Schema exceeds 64 KiB")

    def visit(node, depth=0, refs=()):
        if depth > 16 or not isinstance(node, dict) or set(node) - KEYWORDS:
            raise GatewayError("FEATURE_NOT_SUPPORTED", "Schema is outside the supported V1 subset")
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in refs:
                raise GatewayError("FEATURE_NOT_SUPPORTED", "Only acyclic local $defs references supported")
            name = ref[len("#/$defs/") :]
            if name not in schema.get("$defs", {}):
                raise GatewayError("REQUEST_INVALID", "Unknown schema reference")
            visit(schema["$defs"][name], depth + 1, (*refs, ref))
        for mapping in (node.get("properties", {}), node.get("$defs", {})):
            if not isinstance(mapping, dict):
                raise GatewayError("REQUEST_INVALID", "Invalid schema mapping")
            for child in mapping.values():
                visit(child, depth + 1, refs)
        if "items" in node:
            visit(node["items"], depth + 1, refs)
        if isinstance(node.get("additionalProperties"), dict):
            visit(node["additionalProperties"], depth + 1, refs)
        for child in node.get("anyOf", []):
            visit(child, depth + 1, refs)

    try:
        Draft202012Validator.check_schema(schema)
        visit(schema)
    except (SchemaError, TypeError, KeyError, RecursionError) as exc:
        raise GatewayError("REQUEST_INVALID", "Invalid JSON Schema") from exc


def validate(value, schema, code="OUTPUT_VALIDATION_FAILED"):
    try:
        StrictValidator(schema).validate(value)
    except ValidationError as exc:
        # Do not echo the invalid instance, which may be sensitive business content.
        raise GatewayError(
            code, "Value does not satisfy schema", details={"path": list(exc.absolute_path)}
        ) from exc


def parse_json(text: str, schema: dict | None = None, *, extract=True, code="OUTPUT_VALIDATION_FAILED"):
    if len(text.encode()) > 1048576:
        raise GatewayError(code, "JSON output exceeds 1 MiB")
    candidate = text.strip()
    if extract:
        match = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", candidate)
        if match:
            candidate = match.group(1)
    try:

        def unique(pairs):
            result = {}
            for key, val in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON object key")
                result[key] = val
            return result

        def finite(raw):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("Non-finite number")
            return value

        value = json.loads(
            candidate,
            object_pairs_hook=unique,
            parse_float=finite,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, RecursionError) as exc:
        raise GatewayError(
            "OUTPUT_PARSE_FAILED" if code == "OUTPUT_VALIDATION_FAILED" else code, "Output is not valid JSON"
        ) from exc
    if schema:
        validate(value, schema, code)
    return value
