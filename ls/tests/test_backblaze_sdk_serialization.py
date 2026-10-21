"""Locked botocore serialization contracts for direct Backblaze S3 calls."""
from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import s3  # noqa: E402


def client() -> object:
    return boto3.session.Session().client(
        "s3", endpoint_url="https://s3.us-west-004.backblazeb2.com",
        region_name="us-west-004", aws_access_key_id="id", aws_secret_access_key="secret",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def run(monkeypatch: pytest.MonkeyPatch, operation: str, args: dict, method: str, response: dict, expected: dict) -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(method, response, expected)
        monkeypatch.setattr(s3, "_client", lambda values: sdk)
        s3.execute(operation, args, {})
        stubber.assert_no_pending_responses()


def test_copy_uses_structured_opaque_source_and_sse_c(monkeypatch: pytest.MonkeyPatch) -> None:
    os.environ["B2_TEST_SSE_C"] = base64.b64encode(b"x" * 32).decode()
    args = {"bucket": "dest.bucket", "key": "a b/ü", "source_bucket": "source.bucket", "source_key": "opaque key/ü?%", "source_version_id": "v+/%", "metadata_directive": "REPLACE", "metadata": {"x": "y"}, "encryption": {"algorithm": "SSE-C", "key_env": "B2_TEST_SSE_C"}, "source_encryption": {"algorithm": "SSE-C", "key_env": "B2_TEST_SSE_C"}}
    key = os.environ["B2_TEST_SSE_C"]
    md5 = base64.b64encode(hashlib.md5(base64.b64decode(key)).digest()).decode()
    expected = {"Bucket": "dest.bucket", "Key": "a b/ü", "CopySource": {"Bucket": "source.bucket", "Key": "opaque key/ü?%", "VersionId": "v+/%"}, "MetadataDirective": "REPLACE", "Metadata": {"x": "y"}, "SSECustomerAlgorithm": "AES256", "SSECustomerKey": key, "SSECustomerKeyMD5": md5, "CopySourceSSECustomerAlgorithm": "AES256", "CopySourceSSECustomerKey": key, "CopySourceSSECustomerKeyMD5": md5}
    run(monkeypatch, "CopyObject", args, "copy_object", {"ResponseMetadata": {"HTTPStatusCode": 200}, "CopyObjectResult": {"ETag": "etag"}}, expected)


def test_cors_logging_lock_and_multipart_wire_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    run(monkeypatch, "PutBucketCors", {"bucket": "example.bucket", "cors_rules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://example.invalid"], "AllowedHeaders": ["x-a"], "ExposeHeaders": ["x-b"], "MaxAgeSeconds": 60}]}, "put_bucket_cors", {"ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "CORSConfiguration": {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://example.invalid"], "AllowedHeaders": ["x-a"], "ExposeHeaders": ["x-b"], "MaxAgeSeconds": 60}]}})
    run(monkeypatch, "PutBucketLogging", {"bucket": "example.bucket", "logging": {"LoggingEnabled": {"TargetBucket": "logs.bucket", "TargetPrefix": "audit/"}}}, "put_bucket_logging", {"ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "BucketLoggingStatus": {"LoggingEnabled": {"TargetBucket": "logs.bucket", "TargetPrefix": "audit/"}}})
    run(monkeypatch, "PutObjectLockConfiguration", {"bucket": "example.bucket", "configuration": {"ObjectLockEnabled": "Enabled"}}, "put_object_lock_configuration", {"ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}})
    run(monkeypatch, "CreateMultipartUpload", {"bucket": "example.bucket", "key": "object", "content_type": "text/plain", "metadata": {"a": "b"}}, "create_multipart_upload", {"UploadId": "upload", "ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "Key": "object", "ContentType": "text/plain", "Metadata": {"a": "b"}})


def test_list_and_versioned_delete_optional_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    run(monkeypatch, "ListObjectsV2", {"bucket": "example.bucket", "prefix": "p/", "delimiter": "/", "max_keys": 3, "continuation_token": "opaque+/", "start_after": "after", "encoding_type": "url", "fetch_owner": True, "max_pages": 1}, "list_objects_v2", {"IsTruncated": False, "Contents": [], "ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "Prefix": "p/", "Delimiter": "/", "MaxKeys": 3, "ContinuationToken": "opaque+/", "StartAfter": "after", "EncodingType": "url", "FetchOwner": True})
    run(monkeypatch, "DeleteObjects", {"bucket": "example.bucket", "objects": [{"key": "key", "version_id": "opaque+/"}], "quiet": True}, "delete_objects", {"Deleted": [{"Key": "key", "VersionId": "opaque+/"}], "ResponseMetadata": {"HTTPStatusCode": 200}}, {"Bucket": "example.bucket", "Delete": {"Objects": [{"Key": "key", "VersionId": "opaque+/"}], "Quiet": True}})


def test_all_direct_operation_requests_serialize_before_service_failure(monkeypatch, tmp_path):
    """Stubber returns after real parameter serialization; invalid kwargs fail."""
    import re
    from test_backblaze_matrix import S3_ARGS, _wire_args
    from ls_backblaze.reporting import ToolError
    for name, args in S3_ARGS.items():
        if name in {"PresignGet", "PresignPut", "UploadFileMultipart", "ResumeMultipartUpload"}:
            continue
        method = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        client = s3._client({"endpoint_url": "https://s3.us-west-004.backblazeb2.com", "region_name": "us-west-004", "aws_access_key_id": "explicit", "aws_secret_access_key": "explicit-secret", "verify": True})
        with Stubber(client) as stub:
            stub.add_client_error(method, service_error_code="AccessDenied", http_status_code=403)
            with monkeypatch.context() as patch:
                patch.setattr(s3, "_client", lambda values: client)
                with pytest.raises(ToolError) as raised:
                    s3.execute(name, _wire_args(name, args, tmp_path), {})
            assert raised.value.code == "service_rejected", name
            stub.assert_no_pending_responses()
        client.close()
