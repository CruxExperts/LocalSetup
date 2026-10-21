"""Provider-specific request validation and operation authorization classes."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import admin_schema
from .reporting import ToolError
from .schema_validation import validate as schema_validate
from .storage_shapes import S3, args_schema

ADMIN = admin_schema.OPERATIONS
DESTRUCTIVE = {"s3.DeleteBucket", "s3.DeleteObject", "s3.DeleteObjects", "s3.AbortMultipartUpload", "admin.DeleteBucket", "admin.DeleteKey"}
OVERWRITE = {"PutObject", "PostObject", "CopyObject", "CompleteMultipartUpload", "UploadFileMultipart", "ResumeMultipartUpload", "PresignPut", "PresignPost"}
WRITES = {name for name in S3 if name.startswith(("Put", "Post", "Create", "Delete", "Copy", "Upload", "Complete", "Abort", "Resume"))} | {name for name in ADMIN if not name.startswith(("Get", "List"))}
ACCESS = {name for name in ADMIN if not name.startswith(("Get", "List"))} | {name for name in S3 if name.startswith(("PutBucket", "DeleteBucketCors", "DeleteBucketWebsite", "DeleteBucketLifecycle"))}


def _walk(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise ToolError("invalid_argument", "request nesting exceeds12levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolError("invalid_argument", "non-finite values are not supported")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise ToolError("invalid_argument", "invalid object field name")
            _walk(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 10000:
            raise ToolError("invalid_argument", "array exceeds10000members")
        for item in value:
            _walk(item, depth + 1)


def _date(value: Any) -> None:
    if not isinstance(value, str):
        raise ToolError("invalid_argument", "date must be an RFC3339 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone")
    except ValueError as exc:
        raise ToolError("invalid_argument", "date must be a valid RFC3339 timestamp with timezone") from exc


def _lifecycle(rules: list[dict[str, Any]]) -> None:
    for rule in rules:
        expiration = rule.get("Expiration")
        abort = rule.get("AbortIncompleteMultipartUpload")
        if not expiration and not abort:
            raise ToolError("unsupported_lifecycle", "rule requires expiration or incomplete-multipart abort")
        if expiration:
            values = {key:value for key,value in expiration.items() if value is not None}
            if set(values) not in ({"Date"}, {"Days"}):
                raise ToolError("unsupported_lifecycle", "expiration needs exactly one Date or Days")
            if "Date" in values:
                _date(values["Date"])
            elif type(values["Days"]) is not int or values["Days"] <= 0:
                raise ToolError("invalid_argument", "expiration days must be positive")
        if abort and (type(abort.get("DaysAfterInitiation")) is not int or abort["DaysAfterInitiation"] <= 0):
            raise ToolError("invalid_argument", "multipart abort days must be positive")
        def check_filter(value):
            if not isinstance(value, dict): return
            lower, upper = value.get("ObjectSizeGreaterThan"), value.get("ObjectSizeLessThan")
            if any(item is not None and (type(item) is not int or item < 0) for item in (lower,upper)) or lower is not None and upper is not None and lower >= upper:
                raise ToolError("invalid_argument", "object-size filter bounds are invalid")
            check_filter(value.get("And"))
        check_filter(rule.get("Filter"))


def _admin(name: str, args: dict[str, Any]) -> None:
    if name in {"GetBucketInfo", "GetKeyInfo"}:
        selectors = {"id", "globalAlias", "search"} if name == "GetBucketInfo" else {"id", "search"}
        if len(set(args) & selectors) != 1:
            raise ToolError("invalid_argument", "select exactly one lookup identity")
    if name in {"AllowBucketKey", "DenyBucketKey"}:
        if not args["permissions"] or any(value is not True for value in args["permissions"].values()):
            raise ToolError("invalid_argument", "permission changes require explicit true flags; false is ignored by Garage and rejects here")
    if name == "CreateBucket" and isinstance(args.get("localAlias"), dict):
        permission = args["localAlias"].get("allow", {})
        if not isinstance(permission, dict):
            raise ToolError("invalid_argument", "selected local alias permissions must be an object")
        if any(value is not True for value in permission.values()):
            raise ToolError("invalid_argument", "local alias permission flags must be true when selected")
    if name in {"CreateKey", "UpdateKey"}:
        if "name" in args and args["name"] is None or "neverExpires" in args and args["neverExpires"] is not True:
            raise ToolError("invalid_argument", "selected name must be a string and neverExpires must be true; null/false are ignored")
        for field in ("allow", "deny"):
            if field in args and (not isinstance(args[field], dict) or args[field].get("createBucket") is not True):
                raise ToolError("invalid_argument", "key permission changes require createBucket true")
        if "allow" in args and "deny" in args:
            raise ToolError("invalid_argument", "cannot allow and deny the same key capability")
        if "expiration" in args:
            _date(args["expiration"])
            if args.get("neverExpires"):
                raise ToolError("invalid_argument", "expiration and neverExpires conflict")
        if name == "UpdateKey" and not (set(args) & {"name", "expiration", "allow", "deny"} or args.get("neverExpires")):
            raise ToolError("invalid_argument", "key update requires a selected change")
    if name == "UpdateBucket":
        if set(args) == {"id"} or any(value is None for key,value in args.items() if key != "id"):
            raise ToolError("invalid_argument", "select non-null bucket changes; outer null leaves settings unchanged")
        website = args.get("websiteAccess")
        if website:
            if website["enabled"] and not website.get("indexDocument"):
                raise ToolError("invalid_argument", "enabling website access requires indexDocument")
            if not website["enabled"] and set(website) != {"enabled"}:
                raise ToolError("unsupported_website", "disabling website access only accepts enabled false")
        if "lifecycleRules" in args:
            _lifecycle(args["lifecycleRules"])
        for rule in args.get("corsRules", []):
            if not rule["AllowedOrigin"] or not rule["AllowedMethod"] or any(method not in {"GET","HEAD","PUT","POST","DELETE"} for method in rule["AllowedMethod"]):
                raise ToolError("invalid_argument", "CORS origins/methods must be nonempty and supported")
            if rule.get("MaxAgeSeconds") is not None and rule["MaxAgeSeconds"] < 0:
                raise ToolError("invalid_argument", "CORS max age must be nonnegative")


def validate(operation: str, args: Any) -> tuple[str, str, dict[str, Any]]:
    namespace, separator, name = operation.partition(".")
    if not separator or namespace not in {"s3", "admin"} or name not in (S3 if namespace == "s3" else ADMIN):
        raise ToolError("unsupported_operation", "operation is outside the Garage S3/admin allowlist")
    if not isinstance(args, dict):
        raise ToolError("invalid_argument", "arguments must be a JSON object")
    _walk(args)
    if namespace == "s3" and name in {"PresignGet", "PresignPut", "PresignPost"} and args.get("encryption") and not args.get("secret_output"):
        raise ToolError("secret_output_required", "encrypted presigns require protected secret_output for required headers/form")
    shape = args_schema(name) if namespace == "s3" else admin_schema.args_schema(name)
    schema_validate(args, shape, root={"$defs": admin_schema.definitions()})
    if namespace == "admin":
        _admin(name,args)
        return namespace,name,args
    for field in ("key", "source_key"):
        if field in args and len(args[field].encode()) > 1024:
            raise ToolError("invalid_argument", "object keys are limited to1024UTF-8bytes")
    if "objects" in args and (len({item["key"] for item in args["objects"]}) != len(args["objects"]) or any(len(item["key"].encode()) > 1024 for item in args["objects"])):
        raise ToolError("invalid_argument", "batch keys must be unique and at most1024UTF-8bytes")
    if name == "CopyObject" and "metadata" in args and args.get("metadata_directive") != "REPLACE":
        raise ToolError("invalid_argument", "copy metadata requires REPLACE")
    if "parts" in args:
        numbers = [part["part_number"] for part in args["parts"]]
        if numbers != sorted(set(numbers)):
            raise ToolError("invalid_argument", "multipart parts must be unique and increasing")
    if name == "PutBucketLifecycleConfiguration":
        _lifecycle(args["rules"])
    if name in {"PresignGet", "PresignPut", "PresignPost"}:
        if args.get("encryption") and not args.get("secret_output"):
            raise ToolError("invalid_argument", "encrypted presigns require exclusive secret_output for the URL and required headers/form")
        if "secret_output" in args and not args.get("encryption"):
            raise ToolError("invalid_argument", "secret_output is used only by encrypted presigns")
    if name in {"PresignPost", "PostObject"} and "${filename}" in args["key"]:
        raise ToolError("invalid_argument", "POST requires an exact object key, without filename expansion")
    if name == "PresignPost":
        if args.get("min_content_length",0) > args["max_content_length"] or "${filename}" in args["key"]:
            raise ToolError("invalid_argument", "POST requires an exact key and ordered content-length bounds")
        if args.get("encryption") and not args.get("secret_output"):
            raise ToolError("secret_output_required", "SSE-C POST forms require a protected secret_output file")
    return namespace,name,args


def request_schema():
    branches=[]
    for namespace,table in (("s3",S3),("admin",ADMIN)):
        for name in sorted(table):
            shape=args_schema(name) if namespace=="s3" else admin_schema.args_schema(name)
            branches.append({"type":"object","additionalProperties":False,"required":["operation","args"],"properties":{"operation":{"const":namespace+"."+name},"args":shape}})
    return {"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"https://localsetup.dev/schemas/garage/request-v1.json","oneOf":branches,"$defs":admin_schema.definitions()}
