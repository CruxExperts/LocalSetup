"""Exercise SDK endpoint handlers, not just botocore operation stubs."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def backblaze(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
    return importlib.import_module("ls_backblaze.s3")


def values():
    return {"endpoint_url": "https://s3.us-west-004.backblazeb2.com",
            "region_name": "us-west-004", "aws_access_key_id": "fixture-id",
            "aws_secret_access_key": "fixture-secret", "verify": True}


def test_sdk_optional_checksums_and_addressing_are_explicit(backblaze):
    config = backblaze._client(values()).meta.config
    assert config.s3["addressing_style"] == "path"
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.retries["total_max_attempts"] == 1


class RawResponse:
    def stream(self, *args, **kwargs):
        yield b""


def test_sdk_does_not_follow_s3_region_redirect(backblaze, monkeypatch):
    client = backblaze._client(values())
    sends = []
    def send(request):
        sends.append(request.url)
        return AWSResponse(request.url, 301 if len(sends) == 1 else 200,
                           {"x-amz-bucket-region": "us-east-1", "content-length": "0"}, RawResponse())
    monkeypatch.setattr(client._endpoint.http_session, "send", send)
    try:
        client.head_bucket(Bucket="example-bucket")
    except ClientError:
        pass
    assert len(sends) == 1, "SDK replayed a request after a region redirect"


def test_authorization_response_cannot_redirect_native_credentials(backblaze, monkeypatch):
    transport = importlib.import_module("ls_backblaze.transport")
    client = transport.NativeClient({"endpoint": "https://api.backblazeb2.com",
                                     "key_id": "fixture-id", "application_key": "fixture-secret"})
    client.authorization = {"accountId": "fixture-account", "authorizationToken": "fixture-token",
        "apiInfo": {"storageApi": {"apiUrl": "https://untrusted.example"}}}
    sent = []
    def request(*args, **kwargs):
        sent.append(args)
        return {}
    monkeypatch.setattr(client, "_request", request)
    try:
        client.call("b2_list_buckets", {"accountId": "fixture-account"}, safe=True)
    except Exception as exc:
        assert type(exc).__name__ == "ToolError"
    assert not sent, "authorization-discovered endpoint bypassed the official-host validator"
