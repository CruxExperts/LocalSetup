"""Exhaustive registry matrix for Backblaze S3 and B2-native adapters.

The cases deliberately use optional fields where the adapter has a wire mapping;
they are an offline protocol contract, not a provider integration test.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import native, s3, validation  # noqa: E402
from ls_backblaze.reporting import ToolError  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402


META = {"ResponseMetadata": {}}
S3_ARGS: dict[str, dict[str, Any]] = {
    "ListBuckets": {}, "HeadBucket": {"bucket": "example.bucket"}, "GetBucketLocation": {"bucket": "example.bucket"},
    "CreateBucket": {"bucket": "example.bucket", "object_lock_enabled": True}, "DeleteBucket": {"bucket": "example.bucket"},
    "GetBucketAcl": {"bucket": "example.bucket"}, "GetBucketCors": {"bucket": "example.bucket"}, "GetBucketEncryption": {"bucket": "example.bucket"}, "GetBucketLogging": {"bucket": "example.bucket"}, "GetBucketVersioning": {"bucket": "example.bucket"},
    "PutBucketAcl": {"bucket": "example.bucket", "acl": "private"}, "PutBucketCors": {"bucket": "example.bucket", "cors_rules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://example.invalid"]}]}, "PutBucketEncryption": {"bucket": "example.bucket", "encryption": {"algorithm": "AES256"}}, "DeleteBucketEncryption": {"bucket": "example.bucket"}, "PutBucketLogging": {"bucket": "example.bucket", "logging": {"LoggingEnabled": {"TargetBucket": "log.bucket", "TargetPrefix": "p/"}}}, "DeleteBucketCors": {"bucket": "example.bucket"},
    "ListObjects": {"bucket": "example.bucket", "prefix": "p/", "delimiter": "/", "max_keys": 2, "marker": "m", "encoding_type": "url", "max_pages": 1}, "ListObjectsV2": {"bucket": "example.bucket", "prefix": "p/", "delimiter": "/", "max_keys": 2, "continuation_token": "c", "start_after": "a", "encoding_type": "url", "fetch_owner": True, "max_pages": 1}, "ListObjectVersions": {"bucket": "example.bucket", "prefix": "p/", "delimiter": "/", "max_keys": 2, "key_marker": "k", "version_id_marker": "v", "encoding_type": "url", "max_pages": 1},
    "HeadObject": {"bucket": "example.bucket", "key": "k"}, "GetObject": {"bucket": "example.bucket", "key": "k", "destination": "/tmp/not-used"}, "PutObject": {"bucket": "example.bucket", "key": "k", "source": "/tmp/not-used"}, "CopyObject": {"bucket": "example.bucket", "key": "k", "source_bucket": "source.bucket", "source_key": "source", "source_version_id": "v", "metadata_directive": "COPY"}, "DeleteObject": {"bucket": "example.bucket", "key": "k", "version_id": "v"}, "DeleteObjects": {"bucket": "example.bucket", "objects": [{"key": "k", "version_id": "v"}]},
    "GetObjectAcl": {"bucket": "example.bucket", "key": "k"}, "GetObjectTagging": {"bucket": "example.bucket", "key": "k"}, "PutObjectAcl": {"bucket": "example.bucket", "key": "k", "acl": "private"}, "GetObjectLegalHold": {"bucket": "example.bucket", "key": "k"}, "PutObjectLegalHold": {"bucket": "example.bucket", "key": "k", "status": "ON"}, "GetObjectRetention": {"bucket": "example.bucket", "key": "k"}, "PutObjectRetention": {"bucket": "example.bucket", "key": "k", "retention": {"Mode": "GOVERNANCE", "RetainUntilDate": "2030-01-01T00:00:00Z"}}, "GetObjectLockConfiguration": {"bucket": "example.bucket"}, "PutObjectLockConfiguration": {"bucket": "example.bucket", "configuration": {"ObjectLockEnabled": "Enabled"}},
    "CreateMultipartUpload": {"bucket": "example.bucket", "key": "k", "metadata": {"a": "b"}}, "UploadPart": {"bucket": "example.bucket", "key": "k", "upload_id": "u", "part_number": 1, "source": "/tmp/not-used"}, "UploadPartCopy": {"bucket": "example.bucket", "key": "k", "upload_id": "u", "part_number": 1, "source_bucket": "source.bucket", "source_key": "source"}, "ListParts": {"bucket": "example.bucket", "key": "k", "upload_id": "u"}, "ListMultipartUploads": {"bucket": "example.bucket", "prefix": "p/"}, "CompleteMultipartUpload": {"bucket": "example.bucket", "key": "k", "upload_id": "u", "parts": [{"part_number": 1, "etag": "e"}]}, "AbortMultipartUpload": {"bucket": "example.bucket", "key": "k", "upload_id": "u"},
    "UploadFileMultipart": {"bucket": "example.bucket", "key": "k", "source": "/tmp/not-used", "checkpoint": "/tmp/not-used.checkpoint"}, "ResumeMultipartUpload": {"bucket": "example.bucket", "key": "k", "source": "/tmp/not-used", "checkpoint": "/tmp/not-used.checkpoint"}, "PresignGet": {"bucket": "example.bucket", "key": "k", "expires_seconds": 60}, "PresignPut": {"bucket": "example.bucket", "key": "k", "expires_seconds": 60, "content_type": "text/plain"},
}
NATIVE_ARGS = {"AuthorizeAccount": {}, "ListBuckets": {"bucket_name": "bucket-name"}, "CreateBucket": {"bucket_name": "bucket-name", "bucket_type": "allPrivate"}, "UpdateBucket": {"bucket_id": "id", "if_revision_is": 1, "bucket_type": "allPrivate"}, "DeleteBucket": {"bucket_id": "id"}, "ListKeys": {"max_key_count": 1}, "CreateKey": {"key_name": "key", "capabilities": ["listBuckets"], "secret_output": "/tmp/secret"}, "DeleteKey": {"application_key_id": "id"}, "GetNotificationRules": {"bucket_id": "id"}, "SetNotificationRules": {"bucket_id": "id", "rules": []}}


def test_registry_coverage_is_exact() -> None:
    assert set(S3_ARGS) == set(validation.S3)
    assert set(NATIVE_ARGS) == set(validation.NATIVE)


@pytest.mark.parametrize("name,args", S3_ARGS.items())
def test_each_s3_tool_has_valid_closed_request(name: str, args: dict[str, Any]) -> None:
    validation.validate(f"s3.{name}", args)
    with pytest.raises(ToolError) as error:
        validation.validate(f"s3.{name}", {**args, "unknown": True})
    assert error.value.code == "unknown_field"


@pytest.mark.parametrize("name,args", NATIVE_ARGS.items())
def test_each_native_tool_has_valid_closed_request(name: str, args: dict[str, Any]) -> None:
    validation.validate(f"native.{name}", args)
    with pytest.raises(ToolError) as error:
        validation.validate(f"native.{name}", {**args, "unknown": True})
    assert error.value.code == "unknown_field"


METHOD = {name: "".join("_" + char.lower() if char.isupper() else char for char in name).lstrip("_") for name in S3_ARGS}
METHOD.update({"PresignGet": "generate_presigned_url", "PresignPut": "generate_presigned_url"})
SPECIAL = {"GetObject", "PutObject", "UploadPart", "UploadFileMultipart", "ResumeMultipartUpload"}


class S3Recorder:
    """Offline botocore-client double: records the exact kwargs passed by adapter."""
    def __init__(self) -> None: self.calls: list[tuple[str, dict[str, Any]]] = []
    def __getattr__(self, method: str):
        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((method, kwargs))
            required = {"get_object_tagging": {"TagSet": []}, "delete_objects": {"Deleted": [{"Key": "k", "VersionId": "v"}]}, "head_object": {"ContentLength": 1}, "copy_object": {"CopyObjectResult": {"ETag": "etag"}}, "complete_multipart_upload": {"ETag": "etag"}, "put_object": {"ETag": "etag"}, "list_buckets": {"Buckets": []}, "create_multipart_upload": {"UploadId": "u"}, "upload_part": {"ETag": "e"}, "upload_part_copy": {"CopyPartResult": {"ETag": "e"}}}
            return {**META, **required.get(method, {})}
        return call
    def generate_presigned_url(self, method: str, Params: dict[str, Any], ExpiresIn: int) -> str:
        self.calls.append(("generate_presigned_url", {"method": method, "Params": Params, "ExpiresIn": ExpiresIn}))
        return "https://presigned.invalid/"


def _wire_args(name: str, args: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    copied = dict(args)
    if name in {"PutObject", "UploadPart"}:
        source = tmp_path / f"{name}.bin"; source.write_bytes(b"x"); copied["source"] = str(source)
    return copied


@pytest.mark.parametrize("name,args", [(name, args) for name, args in S3_ARGS.items() if name not in SPECIAL])
def test_every_low_level_s3_operation_dispatches_documented_wire_method(name: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = S3Recorder()
    monkeypatch.setattr(s3, "_client", lambda values: client)
    result = s3.execute(name, _wire_args(name, args, tmp_path), {})
    assert result is not None
    assert client.calls, f"{name} made no SDK call"
    actual_name, actual = client.calls[-1]
    assert actual_name == METHOD[name]
    if "bucket" in args:
        # Botocore parameter names are part of the public adapter contract.
        params = actual.get("Params", actual)
        assert params["Bucket"] == args["bucket"]
    if "key" in args and name not in {"PresignGet", "PresignPut"}:
        assert actual["Key"] == args["key"]


class NativeRecorder:
    def __init__(self, values: dict[str, Any], budget: int): self.calls: list[tuple[str, Any, bool]] = []
    def authorize(self): return {"accountId": "account", "allowed": {"capabilities": ["listBuckets", "writeBuckets", "deleteBuckets", "listKeys", "writeKeys", "deleteKeys", "readBucketNotifications", "writeBucketNotifications", "writeBucketEncryption", "writeBucketRetentions"]}}
    def call(self, operation: str, body: Any, *, safe: bool):
        self.calls.append((operation, body, safe))
        if operation == "b2_list_buckets": return {"buckets": []}
        if operation == "b2_list_keys": return {"keys": []}
        if operation == "b2_create_bucket" or operation == "b2_update_bucket" or operation == "b2_delete_bucket": return {"bucketId": "id"}
        if operation == "b2_delete_key": return {"applicationKeyId": "id"}
        if operation == "b2_set_bucket_notification_rules": return []
        return {"applicationKeyId": "id", "applicationKey": "secret"}
    def get_notification_rules(self, bucket_id: str): self.calls.append(("GET", bucket_id, True)); return []


@pytest.mark.parametrize("name,args", [(name, args) for name, args in NATIVE_ARGS.items() if name not in {"AuthorizeAccount", "CreateKey"}])
def test_every_native_operation_dispatches_v4_body_or_query(name: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = NativeRecorder({}, 1)
    monkeypatch.setattr(native, "NativeClient", lambda values, budget: recorder)
    native.execute(name, args, {}, 1)
    if name == "AuthorizeAccount":
        return
    assert recorder.calls
    operation, body, safe = recorder.calls[-1]
    if name == "GetNotificationRules":
        assert operation == "GET" and body == args["bucket_id"]
    else:
        assert operation.startswith("b2_")
        if name == "SetNotificationRules": assert body == [{"bucketId": args["bucket_id"], "eventNotificationRules": []}]
        elif name == "DeleteKey": assert body == {"applicationKeyId": "id"}
        else: assert body["accountId"] == "account"


@pytest.mark.parametrize("name,args", [(name, args) for name, args in S3_ARGS.items() if name not in SPECIAL])
def test_every_s3_operation_reports_a_known_service_rejection(name: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Rejected:
        def __getattr__(self, method: str):
            def call(**kwargs):
                raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403, "HTTPHeaders": {}}}, method)
            return call
        def generate_presigned_url(self, *args, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403, "HTTPHeaders": {}}}, "Presign")
    monkeypatch.setattr(s3, "_client", lambda values: Rejected())
    with pytest.raises(ToolError) as raised:
        s3.execute(name, _wire_args(name, args, tmp_path), {})
    assert raised.value.code == "service_rejected"


@pytest.mark.parametrize("name,args", [(name, args) for name, args in NATIVE_ARGS.items() if name != "CreateKey"])
def test_every_native_operation_reports_a_known_service_rejection(name: str, args: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    class RejectedNative:
        def __init__(self, values, budget): pass
        def authorize(self):
            if name == "AuthorizeAccount": raise ToolError("service_error", "rejected", "service")
            return {"accountId": "account", "allowed": {"capabilities": ["listBuckets", "writeBuckets", "deleteBuckets", "listKeys", "writeKeys", "deleteKeys", "readBucketNotifications", "writeBucketNotifications", "writeBucketEncryption", "writeBucketRetentions"]}}
        def call(self, *args, **kwargs): raise ToolError("service_error", "rejected", "service")
        def get_notification_rules(self, *args, **kwargs): raise ToolError("service_error", "rejected", "service")
    monkeypatch.setattr(native, "NativeClient", RejectedNative)
    with pytest.raises(ToolError) as raised:
        native.execute(name, args, {}, 1)
    assert raised.value.code == "service_error"
