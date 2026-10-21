"""Dependency-free evaluator for the exact JSON Schema subset we emit."""
from __future__ import annotations

import re
from typing import Any

from .reporting import ToolError


def validate(value: Any, schema: dict[str, Any], path: str = "args") -> None:
    def fail(message: str) -> None:
        raise ToolError("invalid_argument", f"{path}: {message}")
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = 0
            for candidate in schema[keyword]:
                try:
                    validate(value, candidate, path)
                    matches += 1
                except ToolError:
                    pass
            if matches == 0 or (keyword == "oneOf" and matches != 1):
                fail("does not satisfy allowed alternatives")
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if expected and type(value) is not types[expected]:
        fail(f"must be a {expected}")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        fail("unsupported value")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        fail("invalid constant")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            fail("invalid string length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            fail("invalid string pattern")
    if type(value) is int and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        fail("integer out of bounds")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            fail("array length out of bounds")
        if schema.get("uniqueItems") and any(item in value[:index] for index, item in enumerate(value)):
            fail("array members must be unique")
        for index, item in enumerate(value):
            if "items" in schema:
                validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        if not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", float("inf")):
            fail("object size out of bounds")
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value):
            fail("missing required fields")
        additional = schema.get("additionalProperties", True)
        if additional is False and not set(value) <= set(properties):
            raise ToolError("unknown_field", f"{path}: unsupported fields")
        for key, item in value.items():
            if "propertyNames" in schema:
                validate(key, schema["propertyNames"], path + ".<name>")
            if key in properties:
                validate(item, properties[key], f"{path}.{key}")
            elif isinstance(additional, dict):
                validate(item, additional, f"{path}.{key}")
