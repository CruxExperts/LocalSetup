"""Representative exact S3 adapter serialization and failure contracts."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import s3  # noqa: E402
from ls_backblaze.reporting import ToolError  # noqa: E402


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            if name == "create_multipart_upload": return {"UploadId": "upload"}
            if name == "upload_part": return {"ETag": "etag"}
            if name == "upload_part_copy": return {"CopyPartResult": {"ETag": "etag"}}
            if name == "delete_objects": return {"ResponseMetadata": {}, "Deleted": kwargs["Delete"]["Objects"]}
            if name == "copy_object": return {"ResponseMetadata": {}, "CopyObjectResult": {"ETag": "etag"}}
            if name == "put_object": return {"ResponseMetadata": {}, "ETag": "etag"}
            if name == "list_buckets": return {"Buckets": [], "ResponseMetadata": {}}
            return {"ResponseMetadata": {}}
        return method


def use(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3, "_client", lambda values: client)


def test_bucket_configuration_and_batch_delete_wire_params(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client(); use(client, monkeypatch)
    s3.execute("PutBucketCors", {"bucket": "example.bucket", "cors_rules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://example.invalid"]}]}, {})
    assert client.calls.pop() == ("put_bucket_cors", {"Bucket": "example.bucket", "CORSConfiguration": {"CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["https://example.invalid"]}]}})
    s3.execute("DeleteObjects", {"bucket": "example.bucket", "objects": [{"key": "plain"}, {"key": "old", "version_id": "opaque"}]}, {})
    assert client.calls.pop() == ("delete_objects", {"Bucket": "example.bucket", "Delete": {"Objects": [{"Key": "plain"}, {"Key": "old", "VersionId": "opaque"}], "Quiet": False}})


def test_copy_and_multipart_wire_params(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = Client(); use(client, monkeypatch)
    s3.execute("CopyObject", {"bucket": "example.bucket", "key": "target", "source_bucket": "source.bucket", "source_key": "source", "source_version_id": "version", "metadata_directive": "COPY"}, {})
    assert client.calls.pop() == ("copy_object", {"Bucket": "example.bucket", "Key": "target", "CopySource": {"Bucket": "source.bucket", "Key": "source", "VersionId": "version"}, "MetadataDirective": "COPY"})
    s3.execute("CreateMultipartUpload", {"bucket": "example.bucket", "key": "target", "content_type": "text/plain", "metadata": {"a": "b"}}, {})
    assert client.calls.pop() == ("create_multipart_upload", {"Bucket": "example.bucket", "Key": "target", "ContentType": "text/plain", "Metadata": {"a": "b"}})
    source = tmp_path / "part"; source.write_bytes(b"body")
    s3.execute("UploadPart", {"bucket": "example.bucket", "key": "target", "upload_id": "upload", "part_number": 1, "source": str(source)}, {})
    name, params = client.calls.pop()
    assert name == "upload_part"
    assert {key: value for key, value in params.items() if key != "Body"} == {"Bucket": "example.bucket", "Key": "target", "UploadId": "upload", "PartNumber": 1, "ContentLength": 4}


def test_malformed_mutation_response_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def create_multipart_upload(self, **kwargs): return {}
    use(Broken(), monkeypatch)
    with pytest.raises(ToolError) as raised:
        s3.execute("CreateMultipartUpload", {"bucket": "example.bucket", "key": "x"}, {})
    assert raised.value.exit_name == "uncertain"


def test_batch_partial_total_failure_and_malformed_member_accounting(monkeypatch):
    class Batch:
        def __init__(self, result): self.result = result
        def delete_objects(self, **kwargs): return {"ResponseMetadata": {}, **self.result}
    args = {"bucket": "example.bucket", "objects": [{"key": "one"}, {"key": "two"}]}
    failures = [{"Key": "one", "Code": "AccessDenied"}, {"Key": "two", "Code": "AccessDenied"}]
    use(Batch({"Deleted": [{"Key": "one"}], "Errors": [failures[1]]}), monkeypatch)
    assert s3.execute("DeleteObjects", args, {})["partial"] is True
    use(Batch({"Errors": failures}), monkeypatch)
    with pytest.raises(ToolError) as raised: s3.execute("DeleteObjects", args, {})
    assert raised.value.exit_name == "service"
    assert raised.value.as_envelope("s3.DeleteObjects")["error"]["members"] == failures
    use(Batch({"Deleted": [{"Key": "one"}]}), monkeypatch)
    with pytest.raises(ToolError) as raised: s3.execute("DeleteObjects", args, {})
    assert raised.value.exit_name == "uncertain"
