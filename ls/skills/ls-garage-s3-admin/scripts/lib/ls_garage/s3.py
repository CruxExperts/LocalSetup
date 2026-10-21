"""Standalone Garage S3 adapter for the documented compatibility subset."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from typing import Any

from .downloads import download
from .encryption import parameters
from .reporting import ToolError
from .post import presign as presign_post, upload as post_upload
from .responses import s3_response
from .s3_requests import Requests
from .transfers import FileRange, _stat, upload as resumable_upload


def _client(values: dict[str, Any]) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ToolError("dependency_missing", "S3 execution requires the caller-installed requirements-s3-sdk.txt dependency export") from exc
    supplied = {key: value for key, value in values.items() if key != "request_budget_seconds"}
    client = boto3.session.Session().client("s3", config=Config(
        retries={"max_attempts": 0, "mode": "standard"}, connect_timeout=5, read_timeout=60,
        signature_version="s3v4", request_checksum_calculation="when_required",
        response_checksum_validation="when_required", s3={"addressing_style": "path"}), **supplied)
    # S3RegionRedirector otherwise can replay a request after a 301 despite disabled retries.
    emitter = client.meta.events
    for handler in list(emitter._emitter._handlers.prefix_search("needs-retry.s3")):
        owner = getattr(handler, "__self__", None)
        if owner is not None and owner.__class__.__name__.startswith("S3RegionRedirector"):
            emitter.unregister("needs-retry.s3", handler)
    return client


def _params(args: dict[str, Any]) -> dict[str, Any]:
    names = {"bucket": "Bucket", "key": "Key", "prefix": "Prefix", "delimiter": "Delimiter",
             "max_keys": "MaxKeys", "marker": "Marker", "continuation_token": "ContinuationToken",
             "start_after": "StartAfter", "encoding_type": "EncodingType", "fetch_owner": "FetchOwner",
             "upload_id": "UploadId", "part_number": "PartNumber", "max_parts": "MaxParts",
             "part_number_marker": "PartNumberMarker", "key_marker": "KeyMarker",
             "upload_id_marker": "UploadIdMarker", "max_uploads": "MaxUploads", "range": "Range"}
    return {wire: args[field] for field, wire in names.items() if field in args and args[field] is not None}


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


def _list(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    method = getattr(client, "list_objects_v2" if name == "ListObjectsV2" else "list_objects")
    params, pages, seen = _params(args), [], set()
    for _ in range(args.get("max_pages", 1)):
        result = _response(name, method(**params)); pages.append(result)
        if not result.get("IsTruncated"):
            break
        field = "NextContinuationToken" if name == "ListObjectsV2" else "NextMarker"
        token = result.get(field) or ((result.get("Contents") or [{}])[-1].get("Key") if name == "ListObjects" else None)
        if not isinstance(token, str) or not token or token in seen:
            raise ToolError("repeated_continuation", "truncated listing omitted or repeated its continuation", "service")
        seen.add(token); params["ContinuationToken" if name == "ListObjectsV2" else "Marker"] = token
    final = pages[-1] if pages else {}
    return {"pages": pages, "page_count": len(pages), "truncated": bool(final.get("IsTruncated")),
            "next_continuation": final.get("NextContinuationToken") or final.get("NextMarker") or ((final.get("Contents") or [{}])[-1].get("Key") if name == "ListObjects" and final.get("IsTruncated") else None)}


def _put(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    source = Path(args["source"])
    try:
        stream = source.open("rb")
    except OSError as exc:
        raise ToolError("source_missing", "upload source cannot be opened") from exc
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ToolError("source_missing", "upload source must be a regular file")
        if before.st_size > 5 * 1024**3:
            raise ToolError("source_too_large", "single uploads are limited to5GiB; use multipart")
        values = _params(args)
        values.update(Body=FileRange(stream, 0, before.st_size), ContentLength=before.st_size,
                      ContentType=args.get("content_type", "application/octet-stream"), Metadata=args.get("metadata", {}))
        if "content_encoding" in args: values["ContentEncoding"] = args["content_encoding"]
        values.update(parameters(args.get("encryption")))
        result = _response("PutObject", client.put_object(**values))
        changed = _stat(before) != _stat(os.fstat(stream.fileno()))
    return {"etag": result["ETag"], "version_id": None, "checksum": result.get("ChecksumSHA256"),
            "content_length": before.st_size, "assurance": "confirmed one request; ETag is not claimed as a whole-file hash",
            **({"partial": True, "reconciliation": "source changed while upload was in progress; inspect the object"} if changed else {})}


def _presign(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    method = "get_object" if name == "PresignGet" else "put_object"
    values = _params(args); values.update(parameters(args.get("encryption"), customer_only=name == "PresignGet"))
    if name == "PresignPut" and args.get("content_type"): values["ContentType"] = args["content_type"]
    url = client.generate_presigned_url(method, Params=values, ExpiresIn=args["expires_seconds"])
    if args.get("encryption"):
        from .post import HEADERS
        from .secret_files import write
        write(args["secret_output"], {"url": url, "headers": {HEADERS[key]: value for key, value in parameters(args["encryption"]).items()}})
        return {"secret_output": args["secret_output"], "sensitive": True, "expires_seconds": args["expires_seconds"]}
    return {"url": url, "sensitive": True, "expires_seconds": args["expires_seconds"]}


def execute(name: str, args: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Build one path-style SDK client; the Requests wrapper owns all retries."""
    deadline = time.monotonic() + values.get("request_budget_seconds", 300)
    client = Requests(_client(values), deadline)
    if name in {"ListObjects", "ListObjectsV2"}: return _list(client, name, args)
    if name in {"UploadFileMultipart", "ResumeMultipartUpload"}:
        marker = hashlib.sha256("\0".join(values[field] for field in ("endpoint_url", "region_name", "aws_access_key_id")).encode()).hexdigest()
        return resumable_upload(client, args, marker, resume=name == "ResumeMultipartUpload")
    if name == "GetObject": return download(client, args)
    if name == "PutObject": return _put(client, args)
    if name == "PostObject": return post_upload(client, args, values, deadline)
    if name == "PresignPost": return presign_post(client, args)
    if name in {"PresignGet", "PresignPut", "PresignPost"}: return _presign(client, name, args)
    if name == "HeadObject":
        request = _params(args); request.update(parameters(args.get("encryption"), customer_only=True)); return _response(name, client.head_object(**request))
    if name == "DeleteObjects":
        response = _response(name, client.delete_objects(Bucket=args["bucket"], Delete={"Objects": [{"Key": item["key"]} for item in args["objects"]], "Quiet": args.get("quiet", False)}))
        deleted, errors = response.get("Deleted", []), response.get("Errors", [])
        if not isinstance(deleted, list) or not isinstance(errors, list): raise ToolError("malformed_response", "batch deletion result was malformed", "uncertain")
        if any(not isinstance(item, dict) or not isinstance(item.get("Key"), str) for item in deleted + errors) or any(not isinstance(item.get("Code"), str) for item in errors):
            raise ToolError("malformed_response", "batch member response was malformed", "uncertain", False, "inspect each requested object before retrying")
        if len(errors) == len(args["objects"]) and not deleted:
            error = ToolError("batch_failed", "every requested object deletion failed", "service"); error.members = errors; raise error
        if not args.get("quiet") and len(deleted) + len(errors) != len(args["objects"]):
            raise ToolError("malformed_response", "batch response omitted requested members", "uncertain", False, "inspect each requested object before retrying")
        return {"deleted": deleted, "errors": errors, "partial": bool(errors)}
    if name == "CopyObject":
        request = {"Bucket": args["bucket"], "Key": args["key"], "CopySource": {"Bucket": args["source_bucket"], "Key": args["source_key"]}}
        if args.get("metadata_directive"): request["MetadataDirective"] = args["metadata_directive"]
        if args.get("metadata"): request["Metadata"] = args["metadata"]
        request.update(parameters(args.get("encryption"))); request.update(parameters(args.get("source_encryption"), source=True))
        return _response(name, client.copy_object(**request))
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
            request.update(parameters(args.get("encryption"), customer_only=True))
            response = _response(name, client.upload_part(**request))
            if _stat(before) != _stat(os.fstat(stream.fileno())):
                return {"partial": True, "etag": response["ETag"], "upload_id": args["upload_id"],
                        "reconciliation": "source changed during upload; inspect ListParts before completing or uploading another part"}
            return response
    if name == "UploadPartCopy":
        request = {"Bucket": args["bucket"], "Key": args["key"], "UploadId": args["upload_id"], "PartNumber": args["part_number"], "CopySource": {"Bucket": args["source_bucket"], "Key": args["source_key"]}}
        if args.get("source_range"): request["CopySourceRange"] = args["source_range"]
        request.update(parameters(args.get("encryption"), customer_only=True)); request.update(parameters(args.get("source_encryption"), source=True)); return _response(name, client.upload_part_copy(**request))
    if name == "PutBucketCors": return _response(name, client.put_bucket_cors(Bucket=args["bucket"], CORSConfiguration={"CORSRules": args["cors_rules"]}))
    if name == "PutBucketWebsite": return _response(name, client.put_bucket_website(Bucket=args["bucket"], WebsiteConfiguration=args["website"]))
    if name == "PutBucketLifecycleConfiguration": return _response(name, client.put_bucket_lifecycle_configuration(Bucket=args["bucket"], LifecycleConfiguration={"Rules": args["rules"]}))
    if name == "CreateMultipartUpload":
        request = _params(args); request.update(ContentType=args.get("content_type", "application/octet-stream"), Metadata=args.get("metadata", {})); request.update(parameters(args.get("encryption"))); return _response(name, client.create_multipart_upload(**request))
    if name == "CompleteMultipartUpload":
        request = _params(args); request["MultipartUpload"] = {"Parts": [{"PartNumber": part["part_number"], "ETag": part["etag"]} for part in args["parts"]]}; request.update(parameters(args.get("encryption"), customer_only=True)); return _response(name, client.complete_multipart_upload(**request))
    if name == "ListParts":
        request = _params(args); request.update(parameters(args.get("encryption"), customer_only=True)); return _response(name, client.list_parts(**request))
    method = "".join("_" + char.lower() if char.isupper() else char for char in name).lstrip("_")
    return _response(name, getattr(client, method)(**_params(args)))
