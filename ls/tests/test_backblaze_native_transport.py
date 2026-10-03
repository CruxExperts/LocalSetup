"""Native HTTP fixtures with virtual time and exact token-refresh boundaries."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze/scripts/lib"))
from ls_backblaze import native, transport
from ls_backblaze.reporting import ToolError


AUTH = {"accountId": "account", "authorizationToken": "private-token", "apiInfo": {"storageApi": {"apiUrl": "https://api001.backblazeb2.com", "allowed": {"capabilities": ["listBuckets", "writeKeys"]}}}}
VALUES = {"endpoint": "https://api.backblazeb2.com", "key_id": "key", "application_key": "secret"}


class Response(io.BytesIO):
    def __init__(self, url, value):
        super().__init__(json.dumps(value).encode())
        self.url = url
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(request.full_url, value)


def client(monkeypatch, replies, budget=300):
    clock = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transport.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    result = transport.NativeClient(VALUES, budget)
    result.opener = Opener(replies)
    return result, clock


def expired():
    return HTTPError("https://api001.backblazeb2.com", 401, "unauthorized", {}, io.BytesIO(b'{"code":"expired_auth_token"}'))


def test_safe_read_has_three_attempts_and_shared_deadline(monkeypatch):
    instance, clock = client(monkeypatch, [URLError("lost"), URLError("lost"), {"buckets": []}])
    assert instance._request(VALUES["endpoint"], {}, safe=True) == {"buckets": []}
    assert len(instance.opener.requests) == 3
    assert clock[0] == 3
    instance, clock = client(monkeypatch, [URLError("lost")], budget=1)
    with pytest.raises(ToolError):
        instance._request(VALUES["endpoint"], {}, safe=True)
    assert len(instance.opener.requests) == 1


def test_mutation_timeout_and_gateway_failure_never_replay(monkeypatch):
    for failure in (URLError("request sent but response lost"), HTTPError(VALUES["endpoint"], 503, "gateway", {}, io.BytesIO(b""))):
        instance, _ = client(monkeypatch, [failure])
        with pytest.raises(ToolError) as raised:
            instance._request(VALUES["endpoint"], {"bucketId": "id"}, safe=False)
        assert raised.value.exit_name == "uncertain"
        assert len(instance.opener.requests) == 1


def test_token_refresh_is_once_and_notifications_use_get_query(monkeypatch):
    instance, _ = client(monkeypatch, [expired(), AUTH, [{"bucketId": "id/+", "eventNotificationRules": []}]])
    instance.authorization = AUTH
    assert instance.get_notification_rules("id/+")[0]["bucketId"] == "id/+"
    requests = [item[0] for item in instance.opener.requests]
    assert len(requests) == 3
    assert requests[0].method == "GET"
    assert requests[0].data is None
    assert requests[0].full_url.endswith("?bucketId=id%2F%2B")
    instance, _ = client(monkeypatch, [expired(), AUTH, expired()])
    instance.authorization = AUTH
    with pytest.raises(ToolError) as raised:
        instance.call("b2_list_buckets", {"accountId": "account"}, safe=True)
    assert raised.value.code == "authorization_expired"
    assert len(instance.opener.requests) == 3


def test_native_permission_rejection_precedes_mutation(monkeypatch):
    class Client:
        def __init__(self, *args): pass
        def authorize(self): return AUTH
        def call(self, *args, **kwargs): pytest.fail("capability failure must precede dispatch")
    monkeypatch.setattr(native, "NativeClient", Client)
    with pytest.raises(ToolError) as raised:
        native.execute("DeleteBucket", {"bucket_id": "id"}, {}, 300)
    assert raised.value.code == "capability_missing"


def test_resolved_notification_headers_use_native_array_and_bound_secrets(monkeypatch):
    monkeypatch.setenv("HEADER", "protected-value")
    monkeypatch.setenv("HMAC", "a" * 32)
    rule = {"eventTypes": ["b2:ObjectCreated:*"], "isEnabled": True, "name": "rule", "objectNamePrefix": "", "targetConfiguration": {"targetType": "webhook", "url": "https://example.invalid/hook", "customHeaders": {"Authorization": {"env": "HEADER"}}, "hmacSha256SigningSecret": {"env": "HMAC"}}}
    target = native._resolve_notification([rule])[0]["targetConfiguration"]
    assert target["customHeaders"] == [{"name": "Authorization", "value": "protected-value"}]
    monkeypatch.setenv("HEADER", "x" * 2048)
    with pytest.raises(ToolError): native._resolve_notification([rule])
    monkeypatch.setenv("HEADER", "small")
    monkeypatch.setenv("HMAC", "short")
    with pytest.raises(ToolError): native._resolve_notification([rule])


def test_bootstrap_and_failed_refresh_remain_bounded(monkeypatch):
    failures = [HTTPError(VALUES["endpoint"], 503, "temporary", {}, io.BytesIO(b"")) for _ in range(3)]
    instance, _ = client(monkeypatch, [AUTH, expired(), *failures])
    with pytest.raises(ToolError) as raised:
        instance.call("b2_list_buckets", {"accountId": "account"}, safe=True)
    assert raised.value.code == "service_error"
    assert len(instance.opener.requests) == 5


def test_cold_authorization_expired_read_refresh_and_retry_fits_nine_send_budget(monkeypatch):
    replies = [
        URLError("first authorization attempt"),
        URLError("second authorization attempt"),
        AUTH,
        expired(),
        URLError("first refresh authorization attempt"),
        URLError("second refresh authorization attempt"),
        AUTH,
        URLError("second read attempt"),
        {"buckets": []},
    ]
    instance, _ = client(monkeypatch, replies)

    assert instance.call("b2_list_buckets", {"accountId": "account"}, safe=True) == {"buckets": []}
    assert len(instance.opener.requests) == 9
    assert instance.sends == 9
    with pytest.raises(ToolError) as raised:
        instance._once(VALUES["endpoint"], None, basic=True, safe=True)
    assert raised.value.code == "request_attempt_budget_exhausted"
    assert "nine-send" in raised.value.message
    assert len(instance.opener.requests) == 9
