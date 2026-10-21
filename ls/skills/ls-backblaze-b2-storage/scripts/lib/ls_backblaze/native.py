"""Backblaze B2 v4 native administration adapter."""
from __future__ import annotations

import os
import stat
import re
from urllib.parse import quote
from pathlib import Path
from typing import Any

from .reporting import ToolError
from .transport import NativeClient
from .config import resolve
from .native_permissions import check as check_permissions, allowed
from .native_response_shapes import payload as result_shape
from .responses import project

NATIVE_METHODS = {"ListBuckets": "b2_list_buckets", "CreateBucket": "b2_create_bucket", "UpdateBucket": "b2_update_bucket", "DeleteBucket": "b2_delete_bucket", "ListKeys": "b2_list_keys", "CreateKey": "b2_create_key", "DeleteKey": "b2_delete_key", "GetNotificationRules": "b2_get_bucket_notification_rules", "SetNotificationRules": "b2_set_bucket_notification_rules"}


def _validate_result(name: str, value: Any) -> Any:
    safe = name in {"ListBuckets", "ListKeys", "GetNotificationRules"}
    def malformed() -> None:
        raise ToolError("malformed_response", "native response did not satisfy the documented result shape", "service" if safe else "uncertain", False, None if safe else "use the corresponding get/list operation to reconcile the mutation")
    if name in {"GetNotificationRules", "SetNotificationRules"}:
        if not isinstance(value, list) or any(not isinstance(item, dict) or not isinstance(item.get("bucketId"), str) or not isinstance(item.get("eventNotificationRules"), list) for item in value):
            malformed()
        try: return project(value, result_shape(name))
        except ValueError: malformed()
    if not isinstance(value, dict):
        malformed()
    required = {"ListBuckets": ("buckets", list), "ListKeys": ("keys", list), "CreateKey": ("applicationKeyId", str), "DeleteKey": ("applicationKeyId", str), "CreateBucket": ("bucketId", str), "UpdateBucket": ("bucketId", str), "DeleteBucket": ("bucketId", str)}
    field, kind = required[name]
    if not isinstance(value.get(field), kind) or (kind is str and not value[field]):
        malformed()
    if kind is list and any(not isinstance(item, dict) for item in value[field]):
        malformed()
    try: return project(value, result_shape(name, secret=name == "CreateKey"))
    except ValueError: malformed()


def _reserve_secret(path_text: str) -> int:
    path = Path(path_text)
    if any(component.is_symlink() for component in (path, *path.parents)): raise ToolError("secret_output_unsafe", "secret output path must not use symlinks")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            os.close(fd)
            raise
        return fd
    except FileExistsError as exc: raise ToolError("secret_output_exists", "secret output file already exists") from exc
    except OSError as exc: raise ToolError("secret_output_unavailable", "secret output file cannot be reserved") from exc


def _deliver_secret(fd: int, secret: str) -> None:
    try:
        remaining = memoryview((secret + "\n").encode())
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0: raise OSError("secret output made no progress")
            remaining = remaining[written:]
        os.fsync(fd)
    except OSError as exc: raise ToolError("secret_delivery_failed", "remote key exists but its secret could not be durably delivered; revoke it by identifier if necessary", "uncertain", False, "use native.DeleteKey with the created application key id") from exc
    finally: os.close(fd)


def _payload(name: str, args: dict[str, Any], account_id: str) -> dict[str, Any]:
    rename = {"bucket_name": "bucketName", "bucket_type": "bucketType", "bucket_types": "bucketTypes", "bucket_id": "bucketId", "bucket_info": "bucketInfo", "cors_rules": "corsRules", "lifecycle_rules": "lifecycleRules", "default_server_side_encryption": "defaultServerSideEncryption", "default_retention": "defaultRetention", "file_lock_enabled": "fileLockEnabled", "if_revision_is": "ifRevisionIs", "max_key_count": "maxKeyCount", "start_application_key_id": "startApplicationKeyId", "key_name": "keyName", "valid_duration_seconds": "validDurationInSeconds", "bucket_ids": "bucketIds", "name_prefix": "namePrefix", "application_key_id": "applicationKeyId"}
    result = {rename.get(k, k): v for k, v in args.items() if k != "secret_output"}
    if name != "DeleteKey": result["accountId"] = account_id
    if name == "SetNotificationRules": result = {"bucketId": args["bucket_id"], "eventNotificationRules": args["rules"]}
    if name == "GetNotificationRules": result = {"bucketId": args["bucket_id"]}
    return result


def _resolve_notification(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for rule in rules:
        target = dict(rule["targetConfiguration"])
        if "customHeaders" in target:
            pairs = [{"name": key, "value": resolve(reference)} for key, reference in target["customHeaders"].items()]
            if sum(len(quote(item["name"], safe="")) + len(quote(item["value"], safe="")) + 3 for item in pairs) > 2048:
                raise ToolError("invalid_notification_target", "resolved URL-encoded notification headers exceed 2048 bytes")
            target["customHeaders"] = pairs
        signing = target.get("hmacSha256SigningSecret")
        if signing is not None:
            secret = resolve(signing)
            if re.fullmatch(r"[A-Za-z0-9]{32}", secret) is None:
                raise ToolError("invalid_notification_target", "resolved HMAC secret must be exactly 32 alphanumeric characters")
            target["hmacSha256SigningSecret"] = secret
        result.append({**rule, "targetConfiguration": target})
    return result


def execute(name: str, args: dict[str, Any], values: dict[str, Any], budget: int) -> Any:
    client = NativeClient(values, budget)
    auth = client.authorize()
    if name == "AuthorizeAccount":
        return project({"account_id": auth["accountId"], "allowed": allowed(auth), "api_info": auth.get("apiInfo", {})}, result_shape(name))
    check_permissions(name, args, auth)
    secret_fd = _reserve_secret(args["secret_output"]) if name == "CreateKey" else None
    try:
        if name == "GetNotificationRules": data = client.get_notification_rules(args["bucket_id"])
        elif name == "SetNotificationRules":
            body = [{"bucketId": args["bucket_id"], "eventNotificationRules": _resolve_notification(args["rules"])}]
            data = client.call(NATIVE_METHODS[name], body, safe=False)
        else: data = client.call(NATIVE_METHODS[name], _payload(name, args, auth["accountId"]), safe=name in {"ListBuckets", "ListKeys"})
        data = _validate_result(name, data)
        if name == "CreateKey":
            secret = data.pop("applicationKey", None)
            if not isinstance(secret, str) or not secret:
                return {"partial": True, "application_key_id": data["applicationKeyId"], "reconciliation": "creation returned an identifier without its one-time secret; inspect ListKeys and revoke explicitly if appropriate"}
            delivery_fd, secret_fd = secret_fd, None
            try: _deliver_secret(delivery_fd, secret)
            except ToolError:
                return {"partial": True, "application_key_id": data.get("applicationKeyId"), "reconciliation": "secret delivery failed; use native.DeleteKey to revoke the created key"}
        return data
    finally:
        if secret_fd is not None:
            try: os.close(secret_fd)
            except OSError: pass
