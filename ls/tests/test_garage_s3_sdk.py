from __future__ import annotations

import base64
import sys
from pathlib import Path

from botocore.stub import ANY, Stubber

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))

from ls_garage import s3


VALUES = {"endpoint_url": "https://garage.example", "region_name": "garage", "aws_access_key_id": "id", "aws_secret_access_key": "secret", "verify": True, "request_budget_seconds": 20}


def _stub_client() -> tuple[object, Stubber]:
    import boto3
    client = boto3.session.Session(aws_access_key_id="id", aws_secret_access_key="secret", region_name="garage").client("s3", endpoint_url="https://garage.example")
    stubber = Stubber(client); stubber.activate()
    return client, stubber


def test_list_buckets_uses_actual_botocore_client_and_projects_headers(monkeypatch) -> None:
    client, stubber = _stub_client()
    stubber.add_response("list_buckets", {"Buckets": [], "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"authorization": "secret"}}})
    monkeypatch.setattr(s3, "_client", lambda values: client)
    result = s3.execute("ListBuckets", {}, VALUES)
    assert result["Buckets"] == []
    assert "ResponseMetadata" in result and "HTTPHeaders" not in result["ResponseMetadata"]
    stubber.assert_no_pending_responses()


def test_sse_c_is_serialized_for_list_parts_and_completion(monkeypatch, tmp_path: Path) -> None:
    client, stubber = _stub_client(); key = base64.b64encode(b"x" * 32).decode(); monkeypatch.setenv("GARAGE_SSE", key)
    encryption = {"algorithm": "SSE-C", "key_env": "GARAGE_SSE"}
    expected = {"Bucket": "bucket-a", "Key": "key", "UploadId": "upload", "SSECustomerAlgorithm": "AES256", "SSECustomerKey": key, "SSECustomerKeyMD5": ANY}
    stubber.add_response("list_parts", {"Parts": [], "ResponseMetadata": {"HTTPStatusCode": 200}}, expected)
    stubber.add_response("complete_multipart_upload", {"ETag": "etag", "ResponseMetadata": {"HTTPStatusCode": 200}}, {**expected, "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": "part"}]}})
    monkeypatch.setattr(s3, "_client", lambda values: client)
    listed = s3.execute("ListParts", {"bucket": "bucket-a", "key": "key", "upload_id": "upload", "encryption": encryption}, VALUES)
    completed = s3.execute("CompleteMultipartUpload", {"bucket": "bucket-a", "key": "key", "upload_id": "upload", "parts": [{"part_number": 1, "etag": "part"}], "encryption": encryption}, VALUES)
    assert listed["Parts"] == [] and completed["ETag"] == "etag"
    stubber.assert_no_pending_responses()


def test_presigned_post_with_sse_c_writes_raw_key_only_to_protected_file(monkeypatch, tmp_path: Path) -> None:
    client, _ = _stub_client(); key = base64.b64encode(b"y" * 32).decode(); monkeypatch.setenv("GARAGE_POST", key)
    monkeypatch.setattr(s3, "_client", lambda values: client)
    output = tmp_path / "post.json"
    result = s3.execute("PresignPost", {"bucket": "bucket-a", "key": "object", "expires_seconds": 60, "max_content_length": 5, "encryption": {"algorithm": "SSE-C", "key_env": "GARAGE_POST"}, "secret_output": str(output)}, VALUES)
    assert set(result) == {"secret_output", "sensitive", "expires_seconds"}
    assert key in output.read_text()
