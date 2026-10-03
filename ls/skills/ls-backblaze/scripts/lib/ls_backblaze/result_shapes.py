"""Versioned public result envelopes with operation-specific typed payloads."""
from __future__ import annotations

from typing import Any

from .responses import s3_shapes
from .native_response_shapes import payload as native_payload


def object_shape(properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
OBJECT = {"type": "object", "additionalProperties": True}
OBJECTS = {"type": "array", "items": OBJECT}
INTEGER = {"type": "integer", "minimum": 0}
BOOLEAN = {"type": "boolean"}


def payload(namespace: str, name: str) -> dict[str, Any]:
    if namespace == "native":
        return native_payload(name)
    if name in {"PresignGet", "PresignPut"}:
        return object_shape({"url": TEXT, "sensitive": {"const": True}, "expires_seconds": INTEGER}, ("url", "sensitive", "expires_seconds"))
    if name in {"ListObjects", "ListObjectsV2", "ListObjectVersions"}:
        return object_shape({"pages": {"type": "array", "items": s3_shapes()[name]}, "page_count": INTEGER, "truncated": BOOLEAN, "next_continuation": NULL_TEXT}, ("pages", "page_count", "truncated", "next_continuation"))
    if name == "DeleteObjects":
        return object_shape({"deleted": s3_shapes()["DeleteObjects"]["properties"]["Deleted"], "errors": s3_shapes()["DeleteObjects"]["properties"]["Errors"], "partial": BOOLEAN}, ("deleted", "errors", "partial"))
    if name == "GetObjectTagging":
        return object_shape({"tag_set": s3_shapes()["GetObjectTagging"]["properties"]["TagSet"], "compatibility": TEXT}, ("tag_set", "compatibility"))
    if name in {"GetObject", "PutObject", "UploadFileMultipart", "ResumeMultipartUpload"}:
        properties = {"content_length": INTEGER, "version_id": NULL_TEXT, "assurance": TEXT, "etag": NULL_TEXT, "checksum": NULL_TEXT, "destination": TEXT, "backup": NULL_TEXT, "sha256": TEXT, "checksum_verified": BOOLEAN, "upload_id": TEXT, "checkpoint": TEXT}
        if name == "GetObject":
            properties["recovery"] = NULL_TEXT
        return object_shape(properties, ("content_length", "version_id", "assurance"))
    return s3_shapes()[name]


def schema(s3: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    error = {"type": "object", "additionalProperties": False, "required": ["code", "message", "retryable"], "properties": {"code": TEXT, "message": TEXT, "retryable": BOOLEAN, "reconciliation": TEXT, "members": s3_shapes()["DeleteObjects"]["properties"]["Errors"]}}
    branches = []
    for namespace, table in (("s3", s3), ("native", native)):
        for name in table:
            branches.append({"properties": {"operation": {"const": f"{namespace}.{name}"}, "result": payload(namespace, name)}, "required": ["operation", "result"]})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://localsetup.dev/schemas/backblaze/result-v1.json",
        "title": "Backblaze B2 result v1", "type": "object", "additionalProperties": False,
        "required": ["schema_version", "operation", "ok", "outcome"],
        "properties": {"schema_version": {"const": 1}, "operation": TEXT, "ok": BOOLEAN, "outcome": {"enum": ["planned", "succeeded", "partial", "failed", "unknown"]}, "result": {}, "error": error},
        "oneOf": [
            {"properties": {"outcome": {"const": "planned"}, "ok": {"const": True}, "result": object_shape({"network": {"const": False}, "validated": {"const": True}, "apply_required": {"const": True}}, ("network", "validated", "apply_required"))}, "required": ["result"], "not": {"required": ["error"]}},
            {"properties": {"outcome": {"const": "succeeded"}, "ok": {"const": True}}, "not": {"required": ["error"]}, "oneOf": branches},
            {"properties": {"outcome": {"const": "partial"}, "ok": {"const": False}, "result": object_shape({"partial": {"const": True}, "reconciliation": TEXT, "application_key_id": TEXT, "upload_id": TEXT, "version_id": NULL_TEXT, "etag": NULL_TEXT, "checkpoint": TEXT, "content_length": INTEGER, "assurance": TEXT, "checksum": NULL_TEXT, "deleted": s3_shapes()["DeleteObjects"]["properties"]["Deleted"], "errors": s3_shapes()["DeleteObjects"]["properties"]["Errors"]}, ("partial",))}, "required": ["result"], "not": {"required": ["error"]}},
            {"properties": {"outcome": {"enum": ["failed", "unknown"]}, "ok": {"const": False}}, "required": ["error"], "not": {"required": ["result"]}},
        ],
    }
