"""Closed, provider-specific request schemas; no SDK model broadening."""
from __future__ import annotations

import os
import re
import ipaddress
from urllib.parse import urlparse
from typing import Any

from .reporting import ToolError
from .schema_validation import validate as _validate_schema
from .request_shapes import field_schema

NAME = re.compile(r"^(?=.{6,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])$")
KEY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
MAX_DEPTH = 12
S3: dict[str, set[str]] = {
    "ListBuckets": set(), "HeadBucket": {"bucket"}, "GetBucketLocation": {"bucket"},
    "CreateBucket": {"bucket", "object_lock_enabled"}, "DeleteBucket": {"bucket"},
    "GetBucketAcl": {"bucket"}, "GetBucketCors": {"bucket"}, "GetBucketEncryption": {"bucket"}, "GetBucketLogging": {"bucket"}, "GetBucketVersioning": {"bucket"},
    "PutBucketAcl": {"bucket", "acl"}, "PutBucketCors": {"bucket", "cors_rules"}, "DeleteBucketCors": {"bucket"}, "PutBucketEncryption": {"bucket", "encryption"}, "DeleteBucketEncryption": {"bucket"}, "PutBucketLogging": {"bucket", "logging"},
    "ListObjects": {"bucket", "prefix", "delimiter", "max_keys", "marker", "encoding_type", "max_pages"}, "ListObjectsV2": {"bucket", "prefix", "delimiter", "max_keys", "continuation_token", "start_after", "encoding_type", "fetch_owner", "max_pages"}, "ListObjectVersions": {"bucket", "prefix", "delimiter", "max_keys", "key_marker", "version_id_marker", "encoding_type", "max_pages"},
    "HeadObject": {"bucket", "key", "version_id", "encryption"}, "GetObject": {"bucket", "key", "version_id", "destination", "range", "overwrite", "include_recovery_path", "encryption"},
    "PutObject": {"bucket", "key", "source", "content_type", "metadata", "encryption", "content_encoding"},
    "CopyObject": {"bucket", "key", "source_bucket", "source_key", "source_version_id", "metadata_directive", "metadata", "encryption", "source_encryption"},
    "DeleteObject": {"bucket", "key", "version_id"}, "DeleteObjects": {"bucket", "objects", "quiet"},
    "GetObjectAcl": {"bucket", "key", "version_id"}, "GetObjectTagging": {"bucket", "key", "version_id"}, "PutObjectAcl": {"bucket", "key", "version_id", "acl"},
    "GetObjectLegalHold": {"bucket", "key", "version_id"}, "PutObjectLegalHold": {"bucket", "key", "version_id", "status"},
    "GetObjectRetention": {"bucket", "key", "version_id"}, "PutObjectRetention": {"bucket", "key", "version_id", "retention", "bypass_governance"},
    "GetObjectLockConfiguration": {"bucket"}, "PutObjectLockConfiguration": {"bucket", "configuration"},
    "CreateMultipartUpload": {"bucket", "key", "content_type", "metadata", "encryption"}, "UploadPart": {"bucket", "key", "upload_id", "part_number", "source", "encryption"},
    "UploadPartCopy": {"bucket", "key", "upload_id", "part_number", "source_bucket", "source_key", "source_range", "encryption", "source_encryption"}, "ListParts": {"bucket", "key", "upload_id", "max_parts", "part_number_marker"}, "ListMultipartUploads": {"bucket", "key_marker", "upload_id_marker", "prefix", "delimiter", "max_uploads"},
    "CompleteMultipartUpload": {"bucket", "key", "upload_id", "parts"}, "AbortMultipartUpload": {"bucket", "key", "upload_id"},
    "UploadFileMultipart": {"bucket", "key", "source", "checkpoint", "part_size", "content_type", "metadata", "encryption"}, "ResumeMultipartUpload": {"bucket", "key", "source", "checkpoint", "encryption"},
    "PresignGet": {"bucket", "key", "version_id", "expires_seconds", "encryption"}, "PresignPut": {"bucket", "key", "expires_seconds", "content_type", "encryption"},
}
NATIVE: dict[str, set[str]] = {
    "AuthorizeAccount": set(), "ListBuckets": {"bucket_id", "bucket_name", "bucket_types"},
    "CreateBucket": {"bucket_name", "bucket_type", "bucket_info", "cors_rules", "lifecycle_rules", "default_server_side_encryption", "file_lock_enabled"},
    "UpdateBucket": {"bucket_id", "if_revision_is", "bucket_type", "bucket_info", "cors_rules", "lifecycle_rules", "default_server_side_encryption", "default_retention", "file_lock_enabled"}, "DeleteBucket": {"bucket_id"},
    "ListKeys": {"max_key_count", "start_application_key_id"}, "CreateKey": {"key_name", "capabilities", "valid_duration_seconds", "bucket_ids", "name_prefix", "secret_output"}, "DeleteKey": {"application_key_id"},
    "GetNotificationRules": {"bucket_id"}, "SetNotificationRules": {"bucket_id", "rules"},
}
DESTRUCTIVE = {"s3.DeleteBucket", "s3.DeleteObject", "s3.DeleteObjects", "s3.AbortMultipartUpload", "native.DeleteBucket", "native.DeleteKey"}
ACCESS = {name for name in S3 if name.startswith(("PutBucket", "DeleteBucket", "PutObjectAcl", "PutObjectLegalHold", "PutObjectRetention", "PutObjectLock"))} | {"UpdateBucket", "SetNotificationRules"}
WRITES = {name for name in S3 if name.startswith(("Put", "Create", "Copy", "Delete", "Upload", "Complete", "Abort"))} | {"CreateBucket", "UpdateBucket", "DeleteBucket", "CreateKey", "DeleteKey", "SetNotificationRules"}


def operation_parts(operation: str) -> tuple[str, str]:
    try:
        namespace, name = operation.split(".", 1)
    except ValueError as exc:
        raise ToolError("invalid_operation", "operation must be s3.<Operation> or native.<Operation>") from exc
    table = S3 if namespace == "s3" else NATIVE if namespace == "native" else None
    if table is None or name not in table:
        raise ToolError("unsupported_operation", f"unsupported operation: {operation}")
    return namespace, name


def _walk(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ToolError("invalid_argument", "arguments exceed maximum nesting depth")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ToolError("invalid_argument", "arguments cannot contain non-finite numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ToolError("invalid_argument", "object field names must be short strings")
            _walk(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 10000:
            raise ToolError("invalid_argument", "list is too large")
        for item in value: _walk(item, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ToolError("invalid_argument", "argument values must be JSON values")


def _require(args: dict[str, Any], field: str, kind: type = str) -> Any:
    value = args.get(field)
    if type(value) is not kind or (kind is str and not value):
        raise ToolError("invalid_argument", f"{field} is required and must be a non-empty {kind.__name__}")
    return value


def validate(operation: str, args: Any) -> tuple[str, str, dict[str, Any]]:
    namespace, name = operation_parts(operation)
    if not isinstance(args, dict): raise ToolError("invalid_argument", "arguments must be a JSON object")
    _walk(args)
    for field in ("key", "source_key"):
        if isinstance(args.get(field), str) and len(args[field].encode("utf-8")) > 1024:
            raise ToolError("invalid_argument", f"{field} exceeds 1024 UTF-8 bytes")
    allowed = (S3 if namespace == "s3" else NATIVE)[name]
    unknown = set(args) - allowed
    if unknown: raise ToolError("unknown_field", f"unsupported fields: {', '.join(sorted(unknown))}")
    branch = next(item for item in request_schema()["oneOf"] if item["properties"]["operation"]["const"] == operation)
    _validate_schema(args, branch["properties"]["args"], "args")
    if namespace == "s3" and name not in {"ListBuckets"}:
        _require(args, "bucket")
    if namespace == "s3" and name in {"HeadObject", "GetObject", "PutObject", "CopyObject", "DeleteObject", "GetObjectAcl", "GetObjectTagging", "PutObjectAcl", "GetObjectLegalHold", "PutObjectLegalHold", "GetObjectRetention", "PutObjectRetention", "CreateMultipartUpload", "UploadPart", "UploadPartCopy", "ListParts", "CompleteMultipartUpload", "AbortMultipartUpload", "UploadFileMultipart", "ResumeMultipartUpload", "PresignGet", "PresignPut"}: _require(args, "key")
    if name == "GetObject": _require(args, "destination")
    if "bucket" in args and not NAME.fullmatch(args["bucket"]): raise ToolError("invalid_bucket", "bucket must be a 6-63 character lowercase DNS name")
    if "source_bucket" in args and not NAME.fullmatch(args["source_bucket"]): raise ToolError("invalid_bucket", "source_bucket must be a 6-63 character lowercase DNS name")
    for field in ("max_keys", "max_pages", "max_parts", "max_uploads", "part_number", "expires_seconds", "valid_duration_seconds", "if_revision_is"):
        if field in args and (type(args[field]) is not int or args[field] < 0): raise ToolError("invalid_argument", f"{field} must be a non-negative integer")
    if "part_number" in args and not 1 <= args["part_number"] <= 10000: raise ToolError("invalid_argument", "part_number must be 1..10000")
    if "max_pages" in args and not 1 <= args["max_pages"] <= 1000: raise ToolError("invalid_argument", "max_pages must be 1..1000")
    if "expires_seconds" in args and not 1 <= args["expires_seconds"] <= 604800: raise ToolError("invalid_argument", "expires_seconds must be 1..604800")
    if "max_key_count" in args and not 1 <= args["max_key_count"] <= 10000: raise ToolError("invalid_argument", "max_key_count must be 1..10000")
    if name in {"PutBucketAcl", "PutObjectAcl"} and args.get("acl") not in {"private", "public-read"}: raise ToolError("invalid_acl", "Backblaze supports only private or public-read ACL")
    if name == "PutBucketEncryption" and args.get("encryption") not in ({"algorithm": "AES256"}, "AES256"): raise ToolError("unsupported_encryption", "only Backblaze SSE-B2 AES256 bucket encryption is supported")
    if name == "PutObjectLegalHold" and args.get("status") not in {"ON", "OFF"}: raise ToolError("invalid_argument", "legal-hold status must be ON or OFF")
    if name == "DeleteObjects":
        objects = args.get("objects")
        if not isinstance(objects, list) or not objects: raise ToolError("invalid_argument", "objects must be a non-empty list")
        for item in objects:
            if not isinstance(item, dict) or set(item) - {"key", "version_id"} or not isinstance(item.get("key"), str): raise ToolError("invalid_argument", "each object must have key and optional version_id")
    if name == "CompleteMultipartUpload":
        parts = args.get("parts")
        if not isinstance(parts, list) or not parts or any(not isinstance(p, dict) or set(p) != {"part_number", "etag"} or type(p["part_number"]) is not int or not isinstance(p["etag"], str) or not p["etag"] for p in parts) or len({p["part_number"] for p in parts}) != len(parts): raise ToolError("invalid_parts", "parts must be unique non-empty {part_number,etag} objects")
    if name == "UploadFileMultipart":
        _require(args, "source"); _require(args, "checkpoint")
        part_size = args.get("part_size", 5 * 1024 * 1024)
        if type(part_size) is not int or not 5 * 1024 * 1024 <= part_size <= 5 * 1024 * 1024 * 1024: raise ToolError("invalid_argument", "part_size must be 5 MiB through 5 GiB")
    if name == "ResumeMultipartUpload": _require(args, "source"); _require(args, "checkpoint")
    _validate_nested(namespace, name, args)
    if namespace == "native":
        if name == "UpdateBucket":
            _require(args, "bucket_id", str); _require(args, "if_revision_is", int)
            if len(args) == 2: raise ToolError("invalid_argument", "UpdateBucket requires at least one selected mutable field")
            if args.get("file_lock_enabled") is False: raise ToolError("irreversible_setting", "file lock cannot be disabled")
        if name == "CreateKey":
            _require(args, "key_name"); _require(args, "secret_output")
            if not isinstance(args.get("capabilities"), list) or not args["capabilities"]: raise ToolError("invalid_argument", "capabilities must be a non-empty list")
            if not KEY_NAME.fullmatch(args["key_name"]): raise ToolError("invalid_argument", "key_name must be 1-100 safe characters")
            allowed_capabilities={"listKeys","writeKeys","deleteKeys","listAllBucketNames","listBuckets","readBuckets","writeBuckets","deleteBuckets","readBucketRetentions","writeBucketRetentions","readBucketEncryption","writeBucketEncryption","listFiles","readFiles","shareFiles","writeFiles","deleteFiles","readFileLegalHolds","writeFileLegalHolds","readBucketNotifications","readFileRetentions","writeBucketNotifications","writeFileRetentions","bypassGovernance","readBucketLogging","writeBucketLogging"}
            if any(type(item) is not str or item not in allowed_capabilities for item in args["capabilities"]): raise ToolError("invalid_argument", "capabilities contains an unsupported value")
            if "valid_duration_seconds" in args and not 1 <= args["valid_duration_seconds"] <= 86400000: raise ToolError("invalid_argument", "valid_duration_seconds must be 1..86400000")
        if name in {"CreateBucket", "UpdateBucket"} and "bucket_type" in args and args["bucket_type"] not in {"allPrivate", "allPublic"}: raise ToolError("invalid_argument", "bucket_type must be allPrivate or allPublic")
        if name == "SetNotificationRules":
            if not isinstance(args.get("rules"), list): raise ToolError("invalid_argument", "rules must be a list")
            _validate_notification_rules(args["rules"])
    return namespace, name, args



def _closed_object(value: Any, fields: dict[str, type], label: str, *, required: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - set(fields) or (required and not required <= set(value)):
        raise ToolError("invalid_argument", f"{label} has unsupported or missing fields")
    for key, kind in fields.items():
        if key in value and ((isinstance(kind, tuple) and type(value[key]) not in kind) or (not isinstance(kind, tuple) and type(value[key]) is not kind)):
            expected = "/".join(item.__name__ for item in kind) if isinstance(kind, tuple) else kind.__name__
            raise ToolError("invalid_argument", f"{label}.{key} must be a {expected}")
    return value


def _validate_nested(namespace: str, name: str, args: dict[str, Any]) -> None:
    for field in ("encryption", "source_encryption"):
        if field in args: _validate_encryption(args[field])
    if "metadata" in args:
        metadata = args["metadata"]
        if not isinstance(metadata, dict) or len(metadata) > 50 or any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 128 or len(v) > 2048 for k, v in metadata.items()):
            raise ToolError("invalid_metadata", "metadata must contain at most 50 short string pairs")
    if namespace == "s3" and "cors_rules" in args:
        if not args["cors_rules"]: raise ToolError("invalid_argument", "CORS rules must not be empty")
        for rule in args["cors_rules"]:
            _closed_object(rule, {"AllowedHeaders": list, "AllowedMethods": list, "AllowedOrigins": list, "ExposeHeaders": list, "MaxAgeSeconds": int, "ID": str}, "CORS rule", required={"AllowedMethods", "AllowedOrigins"})
            if any(not all(type(item) is str for item in rule[field]) for field in ("AllowedHeaders", "AllowedMethods", "AllowedOrigins", "ExposeHeaders") if field in rule): raise ToolError("invalid_argument", "CORS arrays must contain strings")
    if namespace == "native":
        if "cors_rules" in args:
            for rule in args["cors_rules"]:
                _closed_object(rule, {"corsRuleName": str, "allowedOrigins": list, "allowedHeaders": list, "allowedOperations": list, "exposeHeaders": list, "maxAgeSeconds": int}, "native CORS rule", required={"corsRuleName", "allowedOrigins", "allowedHeaders", "allowedOperations", "exposeHeaders", "maxAgeSeconds"})
                if any(not all(isinstance(item, str) for item in rule[field]) for field in ("allowedOrigins", "allowedHeaders", "allowedOperations", "exposeHeaders")):
                    raise ToolError("invalid_argument", "native CORS rule arrays must contain strings")
        if "lifecycle_rules" in args:
            if not isinstance(args["lifecycle_rules"], list): raise ToolError("invalid_lifecycle", "lifecycle_rules must be an array")
            for rule in args["lifecycle_rules"]:
                _closed_object(rule, {"fileNamePrefix": str, "daysFromHidingToDeleting": (int, type(None)), "daysFromUploadingToHiding": (int, type(None)), "daysFromStartingToCancelingUnfinishedLargeFiles": (int, type(None))}, "lifecycle rule", required={"fileNamePrefix"})
                values = [rule.get(field) for field in ("daysFromHidingToDeleting", "daysFromUploadingToHiding", "daysFromStartingToCancelingUnfinishedLargeFiles")]
                if all(value is None for value in values) or any(value is not None and value <= 0 for value in values): raise ToolError("invalid_lifecycle", "lifecycle rule needs a positive day value")
        if "default_retention" in args:
            retention = _closed_object(args["default_retention"], {"mode": str, "period": dict}, "default retention", required={"mode", "period"})
            if retention["mode"] not in {"governance", "compliance"}: raise ToolError("invalid_retention", "default retention mode must be governance or compliance")
            period = _closed_object(retention["period"], {"duration": int, "unit": str}, "default retention period", required={"duration", "unit"})
            if period["duration"] <= 0 or period["unit"] not in {"days", "years"}: raise ToolError("invalid_retention", "default retention period must be positive days or years")
        if "default_server_side_encryption" in args:
            encryption = _closed_object(args["default_server_side_encryption"], {"mode": str, "algorithm": str}, "default server-side encryption", required={"mode", "algorithm"})
            if (encryption["mode"], encryption["algorithm"]) != ("SSE-B2", "AES256"): raise ToolError("unsupported_encryption", "native default encryption supports only SSE-B2 AES256")
    if namespace == "s3" and name == "PutBucketLogging":
        logging = _closed_object(args["logging"], {"LoggingEnabled": dict}, "logging")
        if logging.get("LoggingEnabled"):
            _closed_object(logging["LoggingEnabled"], {"TargetBucket": str, "TargetPrefix": str}, "logging target", required={"TargetBucket", "TargetPrefix"})
    if name == "PutObjectRetention":
        _closed_object(args["retention"], {"Mode": str, "RetainUntilDate": str}, "retention", required={"Mode", "RetainUntilDate"})
        if args["retention"]["Mode"] not in {"GOVERNANCE", "COMPLIANCE"}: raise ToolError("invalid_retention", "retention Mode must be GOVERNANCE or COMPLIANCE")
    if name == "PutObjectLockConfiguration":
        config = _closed_object(args["configuration"], {"ObjectLockEnabled": str, "Rule": dict}, "object lock configuration", required={"ObjectLockEnabled"})
        if config["ObjectLockEnabled"] != "Enabled": raise ToolError("irreversible_setting", "object lock configuration may only enable lock")
        if config.get("Rule"):
            rule = _closed_object(config["Rule"], {"DefaultRetention": dict}, "object lock default rule", required={"DefaultRetention"})
            _closed_object(rule["DefaultRetention"], {"Mode": str, "Days": int, "Years": int}, "object lock default retention", required={"Mode"})
            retention=rule["DefaultRetention"]
            if retention["Mode"] not in {"GOVERNANCE","COMPLIANCE"} or ("Days" in retention and "Years" in retention) or ("Days" not in retention and "Years" not in retention) or any(retention[key] <= 0 for key in ("Days","Years") if key in retention): raise ToolError("invalid_retention", "lock retention needs exactly one positive Days or Years value")
    if name == "CopyObject" and "metadata" in args and args.get("metadata_directive") != "REPLACE":
        raise ToolError("invalid_argument", "copy metadata requires metadata_directive REPLACE")
    if "retention" in args:
        from datetime import datetime
        try:
            datetime.fromisoformat(args["retention"]["RetainUntilDate"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError("invalid_retention", "retention date must be a real ISO8601 timestamp") from exc
    if name == "CopyObject" and args.get("metadata_directive") not in (None, "COPY", "REPLACE"):
        raise ToolError("invalid_argument", "metadata_directive must be COPY or REPLACE")
    if name in {"PutObject", "UploadPart", "UploadFileMultipart", "ResumeMultipartUpload"}:
        for field in ("source", "checkpoint"):
            if field in args and (not isinstance(args[field], str) or not args[field]): raise ToolError("invalid_argument", f"{field} must be a non-empty path string")


def _validate_encryption(value: Any) -> None:
    if value in ("AES256", {"algorithm": "AES256"}): return
    if not isinstance(value, dict) or set(value) - {"algorithm", "key", "key_env"} or value.get("algorithm") != "SSE-C":
        raise ToolError("unsupported_encryption", "encryption must be AES256 or SSE-C with a protected key reference")
    if "key_env" in value:
        if not isinstance(value["key_env"], str) or not value["key_env"]: raise ToolError("secret_reference_required", "SSE-C key_env must be non-empty")
        return
    key = value.get("key")
    if not isinstance(key, dict) or set(key) not in ({"env"}, {"file"}) or not isinstance(next(iter(key.values())), str) or not next(iter(key.values())):
        raise ToolError("secret_reference_required", "SSE-C key must be {env:NAME} or {file:path}")


def _validate_notification_secrets(value: Any, field: str = "") -> None:
    """Notification signing/header credentials must remain protected references."""
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(word in key_text for word in ("authorization", "secret", "signature", "signing")) and isinstance(item, str):
                raise ToolError("secret_reference_required", f"notification field {key} must use a protected {{env}} or {{file}} reference")
            _validate_notification_secrets(item, key_text)
    elif isinstance(value, list):
        for item in value: _validate_notification_secrets(item, field)


def _validate_notification_rules(rules: list[Any]) -> None:
    for index, rule in enumerate(rules):
        for other in rules[:index]:
            if any(left == right or (left.endswith("*") and right.startswith(left[:-1])) or (right.endswith("*") and left.startswith(right[:-1])) for left in rule["eventTypes"] for right in other["eventTypes"]):
                if rule["objectNamePrefix"].startswith(other["objectNamePrefix"]) or other["objectNamePrefix"].startswith(rule["objectNamePrefix"]):
                    raise ToolError("invalid_notification_rule", "notification prefixes overlap for the same event type")
        if not isinstance(rule, dict) or set(rule) - {"eventTypes", "isEnabled", "name", "objectNamePrefix", "targetConfiguration"}:
            raise ToolError("invalid_notification_rule", "notification rule fields are eventTypes,isEnabled,name,objectNamePrefix,targetConfiguration")
        if set(rule) != {"eventTypes", "isEnabled", "name", "objectNamePrefix", "targetConfiguration"} or not isinstance(rule["eventTypes"], list) or not rule["eventTypes"] or type(rule["isEnabled"]) is not bool or not all(isinstance(rule[x], str) for x in ("name", "objectNamePrefix")):
            raise ToolError("invalid_notification_rule", "notification rule requires eventTypes,isEnabled,name,objectNamePrefix,targetConfiguration")
        target = rule["targetConfiguration"]
        if not isinstance(target, dict) or set(target) - {"targetType", "url", "customHeaders", "hmacSha256SigningSecret", "maxEventsPerBatch"}:
            raise ToolError("invalid_notification_target", "unsupported notification target field")
        if target.get("targetType") != "webhook" or not isinstance(target.get("url"), str) or not target["url"].startswith("https://"):
            raise ToolError("invalid_notification_target", "notification target must be an HTTPS webhook")
        try:
            parsed = urlparse(target["url"])
            host = (parsed.hostname or "").lower()
            _ = parsed.port
        except ValueError as exc:
            raise ToolError("invalid_notification_target", "webhook URL is malformed") from exc
        if not host or parsed.username or parsed.password or parsed.fragment or any(host == domain or host.endswith("." + domain) for domain in ("backblaze.com", "backblazeb2.com")):
            raise ToolError("invalid_notification_target", "webhook URL must not target Backblaze or include user information")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ToolError("invalid_notification_target", "webhook URL cannot target an IP address")
        maximum = target.get("maxEventsPerBatch", 1)
        if type(maximum) is not int or not 1 <= maximum <= 50: raise ToolError("invalid_notification_target", "maxEventsPerBatch must be 1..50")
        headers = target.get("customHeaders", {})
        if not isinstance(headers, dict) or len(headers) > 10 or any(not isinstance(k, str) or not isinstance(v, dict) or set(v) not in ({"env"}, {"file"}) or k.lower().startswith("x-bz-") for k, v in headers.items()):
            raise ToolError("invalid_notification_target", "customHeaders has at most 10 non-X-Bz names with protected references")
        signing = target.get("hmacSha256SigningSecret")
        if signing is not None and (not isinstance(signing, dict) or set(signing) not in ({"env"}, {"file"})):
            raise ToolError("secret_reference_required", "hmacSha256SigningSecret must be null or a protected reference")


def schema() -> dict[str, Any]:
    return {"request": request_schema(), "result": result_schema()}



def _required(namespace: str, name: str) -> list[str]:
    fields = (S3 if namespace == "s3" else NATIVE)[name]
    result: list[str] = []
    if namespace == "s3" and name != "ListBuckets": result.append("bucket")
    if name in {"HeadObject", "GetObject", "PutObject", "CopyObject", "DeleteObject", "GetObjectAcl", "GetObjectTagging", "PutObjectAcl", "GetObjectLegalHold", "PutObjectLegalHold", "GetObjectRetention", "PutObjectRetention", "CreateMultipartUpload", "UploadPart", "UploadPartCopy", "ListParts", "CompleteMultipartUpload", "AbortMultipartUpload", "UploadFileMultipart", "ResumeMultipartUpload", "PresignGet", "PresignPut"}: result.append("key")
    if name == "GetObject": result.append("destination")
    explicit = ({"PutBucketAcl": ["acl"], "PutBucketCors": ["cors_rules"], "PutBucketEncryption": ["encryption"], "PutBucketLogging": ["logging"], "PutObjectAcl": ["acl"], "PutObjectLegalHold": ["status"], "PutObjectRetention": ["retention"], "PutObjectLockConfiguration": ["configuration"], "PutObject": ["source"], "CopyObject": ["source_bucket", "source_key"], "DeleteObjects": ["objects"], "UploadPart": ["upload_id", "part_number", "source"], "UploadPartCopy": ["upload_id", "part_number", "source_bucket", "source_key"], "ListParts": ["upload_id"], "CompleteMultipartUpload": ["upload_id", "parts"], "AbortMultipartUpload": ["upload_id"], "UploadFileMultipart": ["source", "checkpoint"], "ResumeMultipartUpload": ["source", "checkpoint"], "PresignGet": ["expires_seconds"], "PresignPut": ["expires_seconds"]} if namespace == "s3" else {"CreateBucket": ["bucket_name", "bucket_type"], "UpdateBucket": ["bucket_id", "if_revision_is"], "DeleteBucket": ["bucket_id"], "CreateKey": ["key_name", "capabilities", "secret_output"], "DeleteKey": ["application_key_id"], "GetNotificationRules": ["bucket_id"], "SetNotificationRules": ["bucket_id", "rules"]})
    return result + explicit.get(name, [])


def request_schema() -> dict[str, Any]:
    branches = []
    for namespace, table in (("s3", S3), ("native", NATIVE)):
        for name, fields in table.items():
            branches.append({"type": "object", "additionalProperties": False, "required": ["operation", "args"], "properties": {"operation": {"const": f"{namespace}.{name}"}, "args": {"type": "object", "additionalProperties": False, "properties": {field: field_schema(namespace, field) for field in sorted(fields)}, "required": _required(namespace, name)}}})
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://localsetup.dev/schemas/backblaze/request-v1.json", "title": "Backblaze B2 tool request v1", "oneOf": branches}


def result_schema() -> dict[str, Any]:
    from .result_shapes import schema
    return schema(S3, NATIVE)
