"""Capability checks from the explicit B2 authorization response."""
from __future__ import annotations

from typing import Any

from .reporting import ToolError

REQUIRED = {"ListBuckets": "listBuckets", "CreateBucket": "writeBuckets", "UpdateBucket": "writeBuckets", "DeleteBucket": "deleteBuckets", "ListKeys": "listKeys", "CreateKey": "writeKeys", "DeleteKey": "deleteKeys", "GetNotificationRules": "readBucketNotifications", "SetNotificationRules": "writeBucketNotifications"}


def allowed(auth: dict[str, Any]) -> dict[str, Any]:
    storage = auth.get("apiInfo", {}).get("storageApi", {})
    value = storage.get("allowed", auth.get("allowed"))
    if not isinstance(value, dict) or not isinstance(value.get("capabilities"), list) or any(not isinstance(item, str) for item in value["capabilities"]):
        raise ToolError("malformed_response", "authorization response omitted capability information", "service")
    return value


def check(name: str, args: dict[str, Any], auth: dict[str, Any]) -> None:
    capabilities = set(allowed(auth)["capabilities"])
    required = {REQUIRED[name]}
    if "default_server_side_encryption" in args:
        required.add("writeBucketEncryption")
    if "default_retention" in args or args.get("file_lock_enabled"):
        required.add("writeBucketRetentions")
    if not required <= capabilities:
        raise ToolError("capability_missing", "selected key lacks required capability: " + ", ".join(sorted(required - capabilities)), "policy")
