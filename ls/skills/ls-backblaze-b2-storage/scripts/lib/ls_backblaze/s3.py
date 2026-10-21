"""Lazy boto3 S3 adapter; exact public surface is constrained by validation."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Any

from .reporting import ToolError
from .transfers import upload as resumable_upload
from .encryption import parameters as encryption_parameters
from .s3_requests import Requests
from .downloads import download as _download
from .responses import s3_response


def _client(values: dict[str, Any]) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ToolError("dependency_missing", "S3 execution requires the caller-installed requirements-s3-sdk.txt dependency export") from exc
    # Explicit credentials and disabled IMDS prevent implicit AWS discovery.
    client_values = {key: value for key, value in values.items() if key != "request_budget_seconds"}
    client = boto3.session.Session().client(
        "s3",
        config=Config(
            retries={"max_attempts": 0, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
        ),
        **client_values,
    )
    # Botocore's S3RegionRedirector retries a 301 itself even when retries are
    # disabled. A B2 client must send a mutation once, to its configured URL.
    emitter = client.meta.events
    for handler in list(emitter._emitter._handlers.prefix_search("needs-retry.s3")):
        owner = getattr(handler, "__self__", None)
        if owner is not None and owner.__class__.__name__.startswith("S3RegionRedirector"):
            emitter.unregister("needs-retry.s3", handler)
    return client


def _params(args: dict[str, Any]) -> dict[str, Any]:
    names = {"bucket": "Bucket", "key": "Key", "version_id": "VersionId", "prefix": "Prefix", "delimiter": "Delimiter", "max_keys": "MaxKeys", "max_buckets": "MaxBuckets", "continuation_token": "ContinuationToken", "start_after": "StartAfter", "marker": "Marker", "range": "Range", "encoding_type": "EncodingType", "fetch_owner": "FetchOwner", "content_encoding": "ContentEncoding", "upload_id": "UploadId", "part_number": "PartNumber", "max_parts": "MaxParts", "part_number_marker": "PartNumberMarker", "key_marker": "KeyMarker", "version_id_marker": "VersionIdMarker", "upload_id_marker": "UploadIdMarker", "max_uploads": "MaxUploads", "expires_seconds": "ExpiresIn"}
    return {names.get(k, k): v for k, v in args.items() if k in names and v is not None}


def _list_pages(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    method_name = "".join("_" + char.lower() if char.isupper() else char for char in name).lstrip("_")
    method = getattr(client, method_name)
    params = _params(args)
    pages: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for _ in range(args.get("max_pages", 1)):
        response = s3_response(name, method(**params))
        if not isinstance(response, dict): raise ToolError("malformed_response", "list response was malformed", "service")
        pages.append(response)
        if name == "ListObjectsV2":
            token, marker = response.get("NextContinuationToken"), (response.get("NextContinuationToken"), None)
            if not response.get("IsTruncated"): break
            if not isinstance(token, str) or not token: raise ToolError("malformed_response", "truncated listing omitted continuation", "service")
            params["ContinuationToken"] = token
        elif name == "ListObjects":
            token, marker = response.get("NextMarker") or (response.get("Contents") or [{}])[-1].get("Key"), (response.get("NextMarker") or (response.get("Contents") or [{}])[-1].get("Key"), None)
            if not response.get("IsTruncated"): break
            if not isinstance(token, str) or not token: raise ToolError("malformed_response", "truncated listing omitted continuation", "service")
            params["Marker"] = token
        else:
            token, version = response.get("NextKeyMarker"), response.get("NextVersionIdMarker")
            marker = (token, version)
            if not response.get("IsTruncated"): break
            if not isinstance(token, str) or not token: raise ToolError("malformed_response", "truncated listing omitted continuation", "service")
            params["KeyMarker"] = token
            if isinstance(version, str): params["VersionIdMarker"] = version
        if marker in seen: raise ToolError("repeated_continuation", "service repeated a pagination continuation token", "service")
        seen.add(marker)
    final = pages[-1] if pages else {}
    return {"pages": pages, "page_count": len(pages), "truncated": bool(final.get("IsTruncated")), "next_continuation": final.get("NextContinuationToken") or final.get("NextMarker") or final.get("NextKeyMarker")}


def _response(name: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ToolError("malformed_response", "S3 response was malformed", "uncertain" if name.startswith(("Put", "Create", "Copy", "Delete", "Upload", "Complete", "Abort")) else "service")
    required = {"ListBuckets": "Buckets", "PutObject": "ETag", "CreateMultipartUpload": "UploadId", "UploadPart": "ETag", "UploadPartCopy": "CopyPartResult", "CopyObject": "CopyObjectResult", "CompleteMultipartUpload": "ETag", "HeadObject": "ContentLength"}
    field = required.get(name)
    if field and field not in response:
        raise ToolError("malformed_response", f"S3 response omitted required {field}", "uncertain" if name.startswith(("Put", "Create", "Copy", "Delete", "Upload", "Complete", "Abort")) else "service")
    if field:
        value = response[field]
        expected = list if field == "Buckets" else dict if field in {"CopyPartResult", "CopyObjectResult"} else int if field == "ContentLength" else str
        if type(value) is not expected or (expected is str and not value) or (expected is int and value < 0) or (expected is dict and not isinstance(value.get("ETag"), str)):
            raise ToolError("malformed_response", "S3 response contained invalid result fields", "service" if name.startswith(("Get", "Head", "List")) else "uncertain", False, "inspect the corresponding read-only operation")
    if name.startswith(("Head", "Get", "Put", "Delete", "Create", "Copy", "Complete", "Abort", "Upload")) and "ResponseMetadata" not in response and name not in {"CreateMultipartUpload", "UploadPart", "UploadPartCopy"}:
        raise ToolError("malformed_response", "S3 response omitted response metadata", "uncertain" if name.startswith(("Put", "Create", "Copy", "Delete", "Upload", "Complete", "Abort")) else "service")
    return s3_response(name, response)


def _encryption(params: dict[str, Any], value: Any, *, source: bool = False, customer_only: bool = False) -> None:
    params.update(encryption_parameters(value, source=source, customer_only=customer_only))



def _upload(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    from .transfers import FileRange, _stat
    source = Path(args["source"])
    try:
        stream = source.open("rb")
    except OSError as exc:
        raise ToolError("source_missing", "upload source cannot be opened") from exc
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ToolError("source_missing", "source must be a regular file")
        if before.st_size > 5 * 1024**3:
            raise ToolError("source_too_large", "single uploads are limited to 5 GiB; use multipart upload")
        params = _params(args)
        params.update(ContentType=args.get("content_type", "application/octet-stream"), Metadata=args.get("metadata", {}), ContentLength=before.st_size)
        _encryption(params, args.get("encryption"))
        response = _response("PutObject", client.put_object(Body=FileRange(stream, 0, before.st_size), **params))
        changed = _stat(before) != _stat(os.fstat(stream.fileno()))
    result = {"version_id": response.get("VersionId"), "etag": response["ETag"], "checksum": response.get("ChecksumSHA256"), "content_length": before.st_size, "assurance": "confirmed single request and source size; ETag is not claimed as a whole-file hash"}
    if changed:
        result.update(partial=True, reconciliation="source changed during upload; inspect the reported version before replacing it")
    return result


def _execute_once(name: str, args: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    client = Requests(_client(values), time.monotonic() + values.get("request_budget_seconds", 300))
    try:
        if name in {"ListObjects", "ListObjectsV2", "ListObjectVersions"}:
            return _list_pages(client, name, args)
        if name in {"UploadFileMultipart", "ResumeMultipartUpload"}:
            marker = hashlib.sha256(values["aws_access_key_id"].encode()).hexdigest()
            return resumable_upload(client, args, marker, resume=name == "ResumeMultipartUpload")
        if name == "GetObject": return _download(client, args)
        if name == "HeadObject":
            params = _params(args); _encryption(params, args.get("encryption"), customer_only=True)
            return _response(name, client.head_object(**params))
        if name == "PutObject": return _upload(client, args)
        if name == "GetObjectTagging":
            response = s3_response(name, client.get_object_tagging(**_params(args)))
            tags = response.get("TagSet", [])
            if not isinstance(tags, list): raise ToolError("malformed_response", "tagging response was malformed", "service")
            return {"tag_set": tags, "compatibility": "Backblaze documents an empty compatibility tag set"}
        if name in {"PresignGet", "PresignPut"}:
            method = "get_object" if name == "PresignGet" else "put_object"
            params = _params(args); params.pop("ExpiresIn", None); _encryption(params, args.get("encryption"), customer_only=name == "PresignGet")
            if name == "PresignPut" and "content_type" in args: params["ContentType"] = args["content_type"]
            return {"url": client.generate_presigned_url(method, Params=params, ExpiresIn=args["expires_seconds"]), "sensitive": True, "expires_seconds": args["expires_seconds"]}
        if name == "DeleteObjects":
            objects = [{"Key": item["key"], **({"VersionId": item["version_id"]} if "version_id" in item else {})} for item in args["objects"]]
            response = _response(name, client.delete_objects(Bucket=args["bucket"], Delete={"Objects": objects, "Quiet": args.get("quiet", False)}))
            deleted, errors = response.get("Deleted", []), response.get("Errors", [])
            if not isinstance(deleted, list) or not isinstance(errors, list) or any(not isinstance(item, dict) or not isinstance(item.get("Key"), str) for item in deleted + errors) or any(not isinstance(item.get("Code"), str) for item in errors):
                raise ToolError("malformed_response", "batch member response was malformed", "uncertain", False, "inspect each requested object/version before retrying")
            if len(errors) == len(objects) and not deleted:
                error = ToolError("batch_failed", "every requested deletion failed", "service", False)
                error.members = errors
                raise error
            if not args.get("quiet") and len(deleted) + len(errors) != len(objects):
                raise ToolError("malformed_response", "batch response did not account for every requested object", "uncertain", False, "inspect each requested object/version before retrying")
            return {"deleted": deleted, "errors": errors, "partial": bool(errors)}
        if name == "PutBucketAcl" or name == "PutObjectAcl":
            params = _params(args); params["ACL"] = args["acl"]; return _response(name, getattr(client, "put_bucket_acl" if name == "PutBucketAcl" else "put_object_acl")(**params))
        if name == "PutBucketCors": return _response(name, client.put_bucket_cors(Bucket=args["bucket"], CORSConfiguration={"CORSRules": args["cors_rules"]}))
        if name == "PutBucketEncryption": return _response(name, client.put_bucket_encryption(Bucket=args["bucket"], ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}))
        if name == "PutBucketLogging": return _response(name, client.put_bucket_logging(Bucket=args["bucket"], BucketLoggingStatus=args["logging"]))
        if name == "PutObjectLegalHold": return _response(name, client.put_object_legal_hold(Bucket=args["bucket"], Key=args["key"], **({"VersionId": args["version_id"]} if args.get("version_id") else {}), LegalHold={"Status": args["status"]}))
        if name == "PutObjectRetention": return _response(name, client.put_object_retention(Bucket=args["bucket"], Key=args["key"], **({"VersionId": args["version_id"]} if args.get("version_id") else {}), Retention=args["retention"], **({"BypassGovernanceRetention": True} if args.get("bypass_governance") else {})))
        if name == "PutObjectLockConfiguration": return _response(name, client.put_object_lock_configuration(Bucket=args["bucket"], ObjectLockConfiguration=args["configuration"]))
        if name == "CopyObject":
            params = {"Bucket": args["bucket"], "Key": args["key"], "CopySource": {"Bucket": args["source_bucket"], "Key": args["source_key"], **({"VersionId": args["source_version_id"]} if args.get("source_version_id") else {})}}
            if args.get("metadata_directive"): params["MetadataDirective"] = args["metadata_directive"]
            if args.get("metadata"): params["Metadata"] = args["metadata"]
            _encryption(params, args.get("encryption")); _encryption(params, args.get("source_encryption"), source=True)
            return _response(name, client.copy_object(**params))
        if name == "UploadPart":
            from .transfers import FileRange, _stat
            source = Path(args["source"])
            try:
                stream = source.open("rb")
            except OSError as exc:
                raise ToolError("source_missing", "part source cannot be opened") from exc
            with stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > 5 * 1024**3:
                    raise ToolError("source_invalid", "part source must be a regular file no larger than 5 GiB")
                request = _params(args)
                request.update(ContentLength=before.st_size, Body=FileRange(stream, 0, before.st_size))
                request.update(encryption_parameters(args.get("encryption"), customer_only=True))
                response = _response(name, client.upload_part(**request))
                if _stat(before) != _stat(os.fstat(stream.fileno())):
                    return {"partial": True, "etag": response["ETag"], "upload_id": args["upload_id"],
                            "reconciliation": "source changed during upload; inspect ListParts before completing or uploading another part"}
                return response
        if name == "UploadPartCopy":
            params = {"Bucket": args["bucket"], "Key": args["key"], "UploadId": args["upload_id"], "PartNumber": args["part_number"], "CopySource": {"Bucket": args["source_bucket"], "Key": args["source_key"]}}
            if args.get("source_range"): params["CopySourceRange"] = args["source_range"]
            _encryption(params, args.get("encryption"), customer_only=True); _encryption(params, args.get("source_encryption"), source=True)
            return _response(name, client.upload_part_copy(**params))
        method = "".join("_" + char.lower() if char.isupper() else char for char in name).lstrip("_")
        params = _params(args)
        if name == "CreateBucket": params["ObjectLockEnabledForBucket"] = args.get("object_lock_enabled", False)
        if name == "CreateMultipartUpload": params.update({"ContentType": args.get("content_type") or "application/octet-stream", "Metadata": args.get("metadata", {})}); _encryption(params, args.get("encryption"))
        if name == "CompleteMultipartUpload": params["MultipartUpload"] = {"Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in args["parts"]]}
        return _response(name, getattr(client, method)(**params))
    except ToolError:
        raise


def execute(name: str, args: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Request retries live below workflows, so resume cannot replay a write."""
    return _execute_once(name, args, values)
