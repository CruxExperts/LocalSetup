"""Closed Garage admin requests derived from the reviewed v2.3.0 schema."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .reporting import ToolError

OPERATIONS = frozenset("ListBuckets GetBucketInfo CreateBucket DeleteBucket UpdateBucket ListKeys GetKeyInfo CreateKey UpdateKey ImportKey DeleteKey AllowBucketKey DenyBucketKey AddBucketAlias RemoveBucketAlias GetClusterHealth".split())


@lru_cache(maxsize=1)
def source() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "references/admin-api-v2.schema.json"
    return json.loads(path.read_text())


def operation(name: str) -> dict[str, Any]:
    if name not in OPERATIONS:
        raise ToolError("unsupported_operation", "admin operation is outside the allowlist")
    for path, methods in source()["paths"].items():
        for method, value in methods.items():
            if value["operationId"] == name:
                return {**value, "method": method.upper(), "path": path}
    raise ToolError("schema_invalid", "operation is absent from the pinned schema")


def _closed(value: Any) -> Any:
    if isinstance(value, list):
        return [_closed(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _closed(item) for key, item in value.items()}
    if "$ref" in result:
        result["$ref"] = result["$ref"].replace("#/components/schemas/", "#/$defs/")
    if result.get("type") == "object":
        result["additionalProperties"] = False
    return result


def definitions(*, public_result: bool = False) -> dict[str, Any]:
    result = _closed(copy.deepcopy(source()["components"]["schemas"]))
    # Tagged Rust Value(String) and IntValue(i64) are serde newtypes. Their
    # JSON is string/integer; OpenAPI's empty Value shapes are underspecified.
    cors = result["cors.Rule"]["properties"]
    for name in ("AllowedHeader", "AllowedMethod", "AllowedOrigin", "ExposeHeader"):
        cors[name]["items"] = {"type": "string"}
    cors["ID"] = {"type": ["string", "null"]}
    result["lifecycle.Rule"]["properties"]["ID"] = {"type": ["string", "null"]}
    result["lifecycle.Rule"]["properties"]["Status"] = {"enum": ["Enabled", "Disabled"]}
    result["lifecycle.Filter"]["properties"]["Prefix"] = {"type": ["string", "null"]}
    result["lifecycle.Expiration"]["properties"]["Date"] = {"type": ["string", "null"], "format": "date-time"}
    if public_result:
        result["GetKeyInfoResponse"]["properties"].pop("secretAccessKey", None)
    else:
        result["UpdateBucketWebsiteAccess"]["properties"].pop("routingRules", None)
        result["ApiBucketQuotas"]["required"] = ["maxObjects", "maxSize"]
    return result


def expand(shape: dict[str, Any], definitions_value: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in shape:
        shape = definitions_value[shape["$ref"].rsplit("/", 1)[1]]
    return shape


def args_schema(name: str) -> dict[str, Any]:
    op, defs = operation(name), definitions()
    body = _closed(op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {"type": "object", "properties": {}, "additionalProperties": False}))
    body = copy.deepcopy(expand(body, defs))
    if name == "ImportKey":
        return {"type": "object", "additionalProperties": False, "required": ["restore_file", "restore_existing"], "properties": {"restore_existing": {"const": True}, "restore_file": {"type": "object", "additionalProperties": False, "required": ["file"], "properties": {"file": {"type": "string", "minLength": 1}}}}}
    if "oneOf" in body:
        return body
    properties = body.setdefault("properties", {})
    required = body.setdefault("required", [])
    for query in op.get("parameters", []):
        if query["name"] == "showSecretKey":
            continue
        properties[query["name"]] = {**query["schema"], "minLength": 1}
        if query.get("required"):
            required.append(query["name"])
    if name == "CreateKey":
        properties["secret_output"] = {"type": "string", "minLength": 1}
        required.append("secret_output")
    return body


def result_schema(name: str) -> dict[str, Any]:
    response = operation(name)["responses"]["200"]
    return _closed(response.get("content", {}).get("application/json", {}).get("schema", {"type": "object", "additionalProperties": False, "properties": {"deleted": {"type": "string"}}, "required": ["deleted"]}))
