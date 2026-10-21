"""Wire-level adapter tests with local stubs; no provider contact."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import s3  # noqa: E402
from ls_backblaze import native  # noqa: E402
from ls_backblaze.reporting import ToolError  # noqa: E402
from ls_backblaze import transfers  # noqa: E402
from ls_backblaze import validation  # noqa: E402


class TagClient:
    def __init__(self) -> None: self.calls = []
    def get_object_tagging(self, **kwargs): self.calls.append(kwargs); return {"TagSet": []}
    def generate_presigned_url(self, method, Params, ExpiresIn): self.calls.append((method, Params, ExpiresIn)); return "https://sensitive.example/"


def test_get_tagging_dispatches_and_presign_expiry_is_not_api_param(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TagClient(); monkeypatch.setattr(s3, "_client", lambda values: client)
    assert s3.execute("GetObjectTagging", {"bucket": "example.bucket", "key": "x"}, {})["tag_set"] == []
    assert client.calls[0] == {"Bucket": "example.bucket", "Key": "x"}
    result = s3.execute("PresignGet", {"bucket": "example.bucket", "key": "x", "expires_seconds": 60}, {})
    assert result["sensitive"] is True
    assert client.calls[1] == ("get_object", {"Bucket": "example.bucket", "Key": "x"}, 60)


class PageClient:
    def __init__(self) -> None: self.calls = 0
    def list_objects_v2(self, **kwargs):
        self.calls += 1
        return {"IsTruncated": self.calls < 3, "NextContinuationToken": "repeat", "Contents": []}


def test_bounded_pagination_rejects_repeated_continuations(monkeypatch: pytest.MonkeyPatch) -> None:
    client = PageClient(); monkeypatch.setattr(s3, "_client", lambda values: client)
    with pytest.raises(ToolError) as raised:
        s3.execute("ListObjectsV2", {"bucket": "example.bucket", "max_pages": 3}, {})
    assert raised.value.code == "repeated_continuation"


def test_safe_read_retries_but_mutation_is_never_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    from ls_backblaze.s3_requests import Requests
    class Client:
        def __init__(self): self.calls = []
        def list_buckets(self, **kwargs):
            self.calls.append("read")
            if len(self.calls) == 1: raise OSError("connection reset")
            return {"Buckets": []}
        def delete_bucket(self, **kwargs):
            self.calls.append("write")
            raise OSError("response lost")
    client = Client()
    monkeypatch.setattr(s3.time, "sleep", lambda _: None)
    wrapped = Requests(client, s3.time.monotonic() + 300)
    assert wrapped.list_buckets() == {"Buckets": []}
    assert client.calls == ["read", "read"]
    with pytest.raises(ToolError) as raised: wrapped.delete_bucket(Bucket="example.bucket")
    assert raised.value.exit_name == "uncertain"
    assert client.calls == ["read", "read", "write"]


def test_all_matrix_operations_have_closed_schema_branches() -> None:
    request = validation.request_schema()
    branches = {branch["properties"]["operation"]["const"] for branch in request["oneOf"]}
    assert branches == {f"s3.{name}" for name in validation.S3} | {f"native.{name}" for name in validation.NATIVE}
    assert "s3.GetObjectTagging" in branches
    assert "native.SetNotificationRules" in branches


def test_native_bucket_nested_configuration_is_closed() -> None:
    valid = {"bucket_name": "bucket-name", "bucket_type": "allPrivate", "cors_rules": [{"corsRuleName": "r", "allowedOrigins": ["https://example.invalid"], "allowedHeaders": ["x"], "allowedOperations": ["b2_download_file_by_name"], "exposeHeaders": [], "maxAgeSeconds": 60}], "lifecycle_rules": [{"fileNamePrefix": "", "daysFromHidingToDeleting": 1, "daysFromUploadingToHiding": None, "daysFromStartingToCancelingUnfinishedLargeFiles": None}], "default_retention": {"mode": "governance", "period": {"duration": 1, "unit": "days"}}, "default_server_side_encryption": {"mode": "SSE-B2", "algorithm": "AES256"}}
    valid.pop("default_retention")  # Native retention is update-only.
    assert validation.validate("native.CreateBucket", valid)[2] == valid
    broken = dict(valid); broken["lifecycle_rules"] = [{"fileNamePrefix": "", "daysFromHidingToDeleting": 0}]
    with pytest.raises(ToolError) as raised: validation.validate("native.CreateBucket", broken)
    assert raised.value.code == "invalid_argument"


def test_checkpoint_source_identity_prevents_wrong_resume(tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(b"content")
    checkpoint = tmp_path / "checkpoint"
    checkpoint.write_text('{"provider":"backblaze-b2","account":"other","bucket":"example.bucket","key":"x","source":{}}')
    checkpoint.chmod(0o600)
    with pytest.raises(ToolError) as raised:
        transfers.upload(object(), {"bucket": "example.bucket", "key": "x", "source": str(source), "checkpoint": str(checkpoint)}, "account", resume=True)
    assert raised.value.code == "checkpoint_mismatch"


class ReadClient:
    def get_object(self, **kwargs): return {"Body": io.BytesIO(b"content"), "ContentLength": 7, "VersionId": "opaque"}


def test_download_refuses_concurrent_no_clobber(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("present")
    with pytest.raises(ToolError) as raised:
        s3._download(ReadClient(), {"bucket": "example.bucket", "key": "x", "destination": str(target)})
    assert raised.value.code == "destination_exists"
    assert target.read_text() == "present"


class NativeFixture:
    payload = None
    def __init__(self, values, budget): pass
    def authorize(self): return {"accountId": "account", "allowed": {"capabilities": ["listBuckets", "writeBuckets", "deleteBuckets", "listKeys", "writeKeys", "deleteKeys", "readBucketNotifications", "writeBucketNotifications", "writeBucketEncryption", "writeBucketRetentions"]}}
    def call(self, operation, payload, *, safe):
        self.payload = (operation, payload, safe)
        return [] if operation == "b2_set_bucket_notification_rules" else {"ok": True}
    def get_notification_rules(self, bucket_id): return [{"bucketId": bucket_id, "eventNotificationRules": []}]


def test_native_notification_wire_shapes_and_v4_key_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = NativeFixture({}, 0)
    monkeypatch.setattr(native, "NativeClient", lambda values, budget: fixture)
    listed = native.execute("GetNotificationRules", {"bucket_id": "bucket"}, {}, 1)
    assert listed == [{"bucketId": "bucket", "eventNotificationRules": []}]
    rule = {"eventTypes": ["b2:ObjectCreated:*"], "isEnabled": True, "name": "n", "objectNamePrefix": "", "targetConfiguration": {"targetType": "webhook", "url": "https://webhook.example/", "customHeaders": {}, "hmacSha256SigningSecret": None, "maxEventsPerBatch": 1}}
    native.execute("SetNotificationRules", {"bucket_id": "bucket", "rules": [rule]}, {}, 1)
    wire_rule = {**rule, "targetConfiguration": {**rule["targetConfiguration"], "customHeaders": []}}
    assert fixture.payload == ("b2_set_bucket_notification_rules", [{"bucketId": "bucket", "eventNotificationRules": [wire_rule]}], False)
    assert native._payload("CreateKey", {"key_name": "name", "capabilities": ["listBuckets"], "bucket_ids": ["id"], "secret_output": str(tmp_path / "secret")}, "account")["bucketIds"] == ["id"]
    assert "accountId" not in native._payload("DeleteKey", {"application_key_id": "id"}, "account")


def test_key_secret_delivery_failure_is_partial_without_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture = NativeFixture({}, 0)
    fixture.call = lambda operation, payload, safe: {"applicationKey": "one-time-secret", "applicationKeyId": "key-id"}
    monkeypatch.setattr(native, "NativeClient", lambda values, budget: fixture)
    monkeypatch.setattr(native.os, "write", lambda fd, body: (_ for _ in ()).throw(OSError("disk full")))
    result = native.execute("CreateKey", {"key_name": "name", "capabilities": ["listBuckets"], "secret_output": str(tmp_path / "secret")}, {}, 1)
    assert result["partial"] is True
    assert result["application_key_id"] == "key-id"
    assert "one-time-secret" not in repr(result)


class MultipartUnknown:
    def create_multipart_upload(self, **kwargs): return {"UploadId": "upload"}
    def upload_part(self, **kwargs): return {"ETag": "etag"}
    def complete_multipart_upload(self, **kwargs): raise OSError("connection lost")


def test_multipart_completion_unknown_preserves_checkpoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"; source.write_bytes(b"x" * (5 * 1024 * 1024))
    checkpoint = tmp_path / "checkpoint"
    monkeypatch.setattr(s3, "_client", lambda values: MultipartUnknown())
    with pytest.raises(ToolError) as raised:
        s3.execute("UploadFileMultipart", {"bucket": "example.bucket", "key": "x", "source": str(source), "checkpoint": str(checkpoint)}, {"aws_access_key_id": "account"})
    assert raised.value.exit_name == "uncertain"
    assert checkpoint.exists()
