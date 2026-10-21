"""Validate and project provider responses; never expose raw transport headers."""
from __future__ import annotations

import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .reporting import ToolError


@lru_cache(maxsize=1)
def s3_shapes() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("s3-result-shapes.json").read_text())["operations"]


def project(value: Any, shape: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
    root = shape if root is None else root
    if "$ref" in shape:
        selected = root
        for part in shape["$ref"].removeprefix("#/").split("/"):
            selected = selected[part.replace("~1", "/").replace("~0", "~")]
        return project(value, selected, root)
    if isinstance(value, (datetime, date)):
        value = value.isoformat()
    for keyword in ("oneOf", "anyOf"):
        if keyword in shape:
            for branch in shape[keyword]:
                try:
                    return project(value, branch, root)
                except ValueError:
                    pass
            raise ValueError("response alternatives")
    if "const" in shape and (type(value) is not type(shape["const"]) or value != shape["const"]):
        raise ValueError("response constant")
    if "enum" in shape and not any(type(value) is type(item) and value == item for item in shape["enum"]):
        raise ValueError("response enum")
    expected = shape.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None), "number": (int, float)}
    choices = expected if isinstance(expected, list) else [expected] if expected else []
    if choices and not any(isinstance(value, types[item]) if item == "number" else type(value) is types[item] for item in choices):
        raise ValueError("response type")
    if isinstance(value, dict):
        properties = shape.get("properties", {})
        if not set(shape.get("required", [])) <= set(value):
            raise ValueError("response required field")
        result = {}
        additional = shape.get("additionalProperties", False)
        for key, item in value.items():
            if key in properties:
                result[key] = project(item, properties[key], root)
            elif isinstance(additional, dict):
                result[key] = project(item, additional, root)
            elif additional is True:
                result[key] = item
        return result
    if isinstance(value, list):
        return [project(item, shape.get("items", {}), root) for item in value]
    return value


def s3_response(name: str, value: Any) -> dict[str, Any]:
    try:
        return project(value, s3_shapes()[name])
    except (ValueError, TypeError) as exc:
        safe = name.startswith(("Get", "Head", "List"))
        raise ToolError("malformed_response", "S3 response did not satisfy the supported result shape", "service" if safe else "uncertain", False, None if safe else "inspect the corresponding read-only operation before retrying") from exc
