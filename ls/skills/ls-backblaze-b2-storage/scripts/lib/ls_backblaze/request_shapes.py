"""Provider-specific closed request shapes, independent of AWS SDK models."""
from __future__ import annotations

from typing import Any


def string(minimum: int = 1, maximum: int = 4096, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum, **extra}


def integer(minimum: int = 0, maximum: int = 2**63 - 1) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def array(items: dict[str, Any], minimum: int = 0, maximum: int = 10000) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


BUCKET = string(6, 63, pattern=r"^(?!b2-)(?!.*\.\.)(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9][a-z0-9.-]*[a-z0-9]$")
NATIVE_BUCKET = string(6, 63, pattern=r"^(?![bB]2-)(?!.*\.\.)(?!\d+\.\d+\.\d+\.\d+$)[A-Za-z0-9-][A-Za-z0-9.-]*[A-Za-z0-9-]$")
REF = {"oneOf": [obj({"env": string()}, ("env",)), obj({"file": string()}, ("file",))]}
ENCRYPTION = {"oneOf": [{"const": "AES256"}, obj({"algorithm": {"const": "AES256"}}, ("algorithm",)), obj({"algorithm": {"const": "SSE-C"}, "key_env": string()}, ("algorithm", "key_env")), obj({"algorithm": {"const": "SSE-C"}, "key": REF}, ("algorithm", "key"))]}
CAPABILITIES = "listKeys writeKeys deleteKeys listAllBucketNames listBuckets readBuckets writeBuckets deleteBuckets readBucketRetentions writeBucketRetentions readBucketEncryption writeBucketEncryption listFiles readFiles shareFiles writeFiles deleteFiles readFileLegalHolds writeFileLegalHolds readBucketNotifications readFileRetentions writeBucketNotifications writeFileRetentions bypassGovernance readBucketLogging writeBucketLogging".split()
MODE = {"enum": ["GOVERNANCE", "COMPLIANCE"]}
LOCK_RETENTION = {"oneOf": [obj({"Mode": MODE, "Days": integer(1)}, ("Mode", "Days")), obj({"Mode": MODE, "Years": integer(1)}, ("Mode", "Years"))]}
METADATA = {"type": "object", "maxProperties": 50, "propertyNames": string(1, 128), "additionalProperties": string(0, 2048)}
NATIVE_CORS = obj({"corsRuleName": string(), "allowedOrigins": array(string(), 1), "allowedHeaders": array(string()), "allowedOperations": array(string(), 1), "exposeHeaders": array(string()), "maxAgeSeconds": integer()}, ("corsRuleName", "allowedOrigins", "allowedHeaders", "allowedOperations", "exposeHeaders", "maxAgeSeconds"))
S3_CORS = obj({"AllowedHeaders": array(string()), "AllowedMethods": array({"enum": ["GET", "PUT", "HEAD", "POST", "DELETE"]}, 1), "AllowedOrigins": array(string(), 1), "ExposeHeaders": array(string()), "MaxAgeSeconds": integer(), "ID": string()}, ("AllowedMethods", "AllowedOrigins"))
LIFECYCLE = obj({"fileNamePrefix": string(0), **{field: {"anyOf": [integer(1), {"type": "null"}]} for field in ("daysFromHidingToDeleting", "daysFromUploadingToHiding", "daysFromStartingToCancelingUnfinishedLargeFiles")}}, ("fileNamePrefix",))
LIFECYCLE["anyOf"] = [{"required": [field], "properties": {field: integer(1)}} for field in ("daysFromHidingToDeleting", "daysFromUploadingToHiding", "daysFromStartingToCancelingUnfinishedLargeFiles")]
NOTIFICATION_TARGET = obj({"targetType": {"const": "webhook"}, "url": string(pattern=r"^https://[^\s]+$"), "maxEventsPerBatch": integer(1, 50), "customHeaders": {"type": "object", "maxProperties": 10, "propertyNames": string(), "additionalProperties": REF}, "hmacSha256SigningSecret": {"anyOf": [REF, {"type": "null"}]}}, ("targetType", "url"))
NOTIFICATION = obj({"eventTypes": array(string(), 1), "isEnabled": {"type": "boolean"}, "name": string(), "objectNamePrefix": string(0), "targetConfiguration": NOTIFICATION_TARGET}, ("eventTypes", "isEnabled", "name", "objectNamePrefix", "targetConfiguration"))


def field_schema(namespace: str, field: str) -> dict[str, Any]:
    counts = {"max_buckets": (1, 10000), "max_keys": (0, 1000), "max_pages": (1, 1000), "max_parts": (1, 1000), "max_uploads": (1, 1000), "max_key_count": (1, 10000), "part_number": (1, 10000), "part_number_marker": (0, 10000), "part_size": (5 * 1024**2, 5 * 1024**3), "expires_seconds": (1, 604800), "valid_duration_seconds": (1, 86400000), "if_revision_is": (0, 2**63 - 1)}
    if field in counts:
        return integer(*counts[field])
    if field in {"object_lock_enabled", "overwrite", "include_recovery_path", "quiet", "fetch_owner", "bypass_governance", "allow_overwrite", "file_lock_enabled"}:
        return {"type": "boolean"}
    if field in {"bucket", "bucket_name", "source_bucket"}:
        return NATIVE_BUCKET if namespace == "native" else BUCKET
    if field in {"encryption", "source_encryption"}:
        return ENCRYPTION
    shapes = {
        "key": string(1, 1024), "source_key": string(1, 1024),
        "prefix": string(0), "name_prefix": string(0), "delimiter": string(0), "start_after": string(0),
        "bucket_type": {"enum": ["allPrivate", "allPublic"]},
        "bucket_types": array({"enum": ["allPrivate", "allPublic", "snapshot", "*"]}, 1),
        "key_name": string(1, 100, pattern=r"^[A-Za-z0-9-]+$"),
        "capabilities": {**array({"enum": CAPABILITIES}, 1), "uniqueItems": True},
        "bucket_ids": {**array(string(), 1), "uniqueItems": True},
        "metadata": METADATA, "bucket_info": {"type": "object"},
        "cors_rules": array(NATIVE_CORS if namespace == "native" else S3_CORS, 0 if namespace == "native" else 1, 100),
        "lifecycle_rules": array(LIFECYCLE),
        "default_retention": obj({"mode": {"enum": ["governance", "compliance"]}, "period": obj({"duration": integer(1), "unit": {"enum": ["days", "years"]}}, ("duration", "unit"))}, ("mode", "period")),
        "default_server_side_encryption": obj({"mode": {"const": "SSE-B2"}, "algorithm": {"const": "AES256"}}, ("mode", "algorithm")),
        "objects": array(obj({"key": string(1, 1024), "version_id": string()}, ("key",)), 1, 1000),
        "parts": array(obj({"part_number": integer(1, 10000), "etag": string()}, ("part_number", "etag")), 1),
        "rules": array(NOTIFICATION), "acl": {"enum": ["private", "public-read"]},
        "status": {"enum": ["ON", "OFF"]}, "metadata_directive": {"enum": ["COPY", "REPLACE"]},
        "encoding_type": {"const": "url"},
        "logging": obj({"LoggingEnabled": obj({"TargetBucket": BUCKET, "TargetPrefix": string(0)}, ("TargetBucket", "TargetPrefix"))}),
        "retention": obj({"Mode": MODE, "RetainUntilDate": string(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")}, ("Mode", "RetainUntilDate")),
        "configuration": obj({"ObjectLockEnabled": {"const": "Enabled"}, "Rule": obj({"DefaultRetention": LOCK_RETENTION}, ("DefaultRetention",))}, ("ObjectLockEnabled",)),
        "range": string(pattern=r"^bytes=(?:\d+-\d*|-\d+)$"),
        "source_range": string(pattern=r"^bytes=\d+-\d+$"),
    }
    return shapes.get(field, string())
