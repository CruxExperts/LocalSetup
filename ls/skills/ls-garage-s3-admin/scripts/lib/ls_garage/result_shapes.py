"""Closed public result-envelope schema for the Garage operation allowlist."""
from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION, admin_schema
from .responses import s3_shapes
from .storage_shapes import S3

TEXT = {"type": "string"}; BOOL = {"type": "boolean"}; INTEGER = {"type": "integer", "minimum": 0}; NULL_TEXT = {"type": ["string", "null"]}


def obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


def payload(namespace: str, name: str) -> dict[str, Any]:
    if namespace == "admin": return admin_schema.result_schema(name)
    if name in {"PresignGet", "PresignPut"}:
        return {"oneOf": [obj({"url": TEXT, "sensitive": {"const": True}, "expires_seconds": INTEGER}, ("url", "sensitive", "expires_seconds")), obj({"secret_output": TEXT, "sensitive": {"const": True}, "expires_seconds": INTEGER}, ("secret_output", "sensitive", "expires_seconds"))]}
    if name == "PresignPost":
        return {"oneOf": [
            obj({"url": TEXT, "fields": {"type": "object", "additionalProperties": TEXT}, "sensitive": {"const": True}, "expires_seconds": INTEGER}, ("url", "fields", "sensitive", "expires_seconds")),
            obj({"secret_output": TEXT, "sensitive": {"const": True}, "expires_seconds": INTEGER}, ("secret_output", "sensitive", "expires_seconds")),
        ]}
    if name == "PostObject": return obj({"etag": NULL_TEXT, "content_length": INTEGER, "assurance": TEXT}, ("content_length", "assurance"))
    if name in {"ListObjects", "ListObjectsV2"}: return obj({"pages": {"type": "array", "items": s3_shapes()[name]}, "page_count": INTEGER, "truncated": BOOL, "next_continuation": NULL_TEXT}, ("pages", "page_count", "truncated", "next_continuation"))
    if name == "DeleteObjects":
        shape = s3_shapes()[name]["properties"]
        return obj({"deleted": shape["Deleted"], "errors": shape["Errors"], "partial": BOOL}, ("deleted", "errors", "partial"))
    if name in {"GetObject", "PutObject", "UploadFileMultipart", "ResumeMultipartUpload"}:
        return obj({"destination": TEXT, "backup": NULL_TEXT, "content_length": INTEGER, "version_id": NULL_TEXT, "checksum": NULL_TEXT, "checksum_verified": BOOL, "sha256": TEXT, "etag": NULL_TEXT, "upload_id": TEXT, "checkpoint": TEXT, "assurance": TEXT}, ("content_length", "assurance"))
    return s3_shapes()[name]


def schema() -> dict[str, Any]:
    batch = s3_shapes()["DeleteObjects"]["properties"]
    error = obj({"code": TEXT, "message": TEXT, "retryable": BOOL, "reconciliation": TEXT, "members": batch["Errors"]}, ("code", "message", "retryable"))
    success = []
    for namespace, names in (("s3", S3), ("admin", admin_schema.OPERATIONS)):
        for name in sorted(names): success.append({"properties": {"operation": {"const": f"{namespace}.{name}"}, "result": payload(namespace, name)}, "required": ["operation", "result"]})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://localsetup.dev/schemas/garage/result-v1.json", "title": "Garage result v1", "$defs": admin_schema.definitions(public_result=True), "type": "object", "additionalProperties": False, "required": ["schema_version", "operation", "ok", "outcome"], "properties": {"schema_version": {"const": SCHEMA_VERSION}, "operation": TEXT, "ok": BOOL, "outcome": {"enum": ["planned", "succeeded", "partial", "failed", "unknown"]}, "result": {}, "error": error}, "oneOf": [{"properties": {"outcome": {"const": "planned"}, "ok": {"const": True}, "result": obj({"network": {"const": False}, "validated": {"const": True}, "apply_required": {"const": True}}, ("network", "validated", "apply_required"))}, "required": ["result"], "not": {"required": ["error"]}}, {"properties": {"outcome": {"const": "succeeded"}, "ok": {"const": True}}, "not": {"required": ["error"]}, "oneOf": success}, {"properties": {"outcome": {"const": "partial"}, "ok": {"const": False}, "result": obj({"partial": {"const": True}, "reconciliation": TEXT, "access_key_id": TEXT, "upload_id": TEXT, "version_id": NULL_TEXT, "etag": NULL_TEXT, "checksum": NULL_TEXT, "checkpoint": TEXT, "content_length": INTEGER, "assurance": TEXT, "deleted": batch["Deleted"], "errors": batch["Errors"]}, ("partial",))}, "required": ["result"], "not": {"required": ["error"]}}, {"properties": {"outcome": {"enum": ["failed", "unknown"]}, "ok": {"const": False}}, "required": ["error"], "not": {"required": ["result"]}}]}
