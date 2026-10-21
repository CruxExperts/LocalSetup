"""Offline Garage Admin API transport-boundary regressions."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALUES = {
    "endpoint": "https://admin.example",
    "token": "test-token",
    "request_budget_seconds": 1,
}


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))
    return importlib.import_module("ls_garage.admin")


class Response:
    """Minimal context-managed urllib response without a network socket."""

    def __init__(self, value: str):
        self.value = io.BytesIO(value.encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=None):
        return self.value.read(_limit)


def test_preexisting_secret_output_prevents_create_key_dispatch(admin, monkeypatch, tmp_path):
    """A key secret must have a new protected destination before POST dispatch."""

    target = tmp_path / "existing-key.json"
    target.write_text("already present")
    dispatches = []

    class Opener:
        def open(self, *args, **kwargs):
            dispatches.append((args, kwargs))
            raise AssertionError("CreateKey dispatched after secret reservation failed")

    monkeypatch.setattr(admin, "build_opener", lambda *args: Opener())

    with pytest.raises(admin.ToolError) as raised:
        admin.execute(
            "CreateKey",
            {"name": "test-key", "secret_output": str(target)},
            VALUES,
        )

    assert raised.value.code == "secret_file_exists"
    assert dispatches == []


def test_deadline_prevents_dispatch_after_retry_wait(admin, monkeypatch):
    """A read retry must recheck the common deadline before opening a request."""

    dispatches = []
    clock = iter([0, 0, 0, 2])
    monkeypatch.setattr(admin.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(admin.time, "sleep", lambda _delay: None)

    class Opener:
        def open(self, *args, **kwargs):
            dispatches.append((args, kwargs))
            raise HTTPError(
                "https://admin.example/v2/ListBuckets", 502, "bad gateway", {}, None
            )

    monkeypatch.setattr(admin, "build_opener", lambda *args: Opener())

    with pytest.raises(admin.ToolError) as raised:
        admin.execute("ListBuckets", {}, VALUES)

    assert raised.value.code == "request_budget_expired"
    assert dispatches and len(dispatches) == 1


def test_post_502_is_unknown_and_never_replayed(admin, monkeypatch):
    """Ambiguous Admin writes must expose reconciliation, never an automatic replay."""

    dispatches = []

    class Opener:
        def open(self, *args, **kwargs):
            dispatches.append((args, kwargs))
            raise HTTPError(
                "https://admin.example/v2/CreateBucket", 502, "bad gateway", {}, None
            )

    monkeypatch.setattr(admin, "build_opener", lambda *args: Opener())

    with pytest.raises(admin.ToolError) as raised:
        admin.execute("CreateBucket", {"name": "test-bucket"}, VALUES)

    assert raised.value.code == "write_outcome_unknown"
    assert raised.value.exit_name == "uncertain"
    assert raised.value.reconciliation
    assert len(dispatches) == 1


def test_get_key_info_forces_false_and_projects_secret(admin, monkeypatch):
    """GetKeyInfo cannot request or surface an Admin API secret key."""

    requests = []

    class Opener:
        def open(self, request, **kwargs):
            requests.append((request, kwargs))
            response = Response(
                json.dumps(
                    {
                        "accessKeyId": "key-id",
                        "name": "test-key",
                        "expired": False,
                        "permissions": {},
                        "buckets": [],
                        "secretAccessKey": "must-not-leak",
                    }
                )
            )
            response.geturl = lambda: request.full_url
            return response

    monkeypatch.setattr(admin, "build_opener", lambda *args: Opener())

    result = admin.execute(
        "GetKeyInfo", {"id": "key-id", "showSecretKey": True}, VALUES
    )

    query = parse_qs(urlsplit(requests[0][0].full_url).query)
    assert query["id"] == ["key-id"]
    assert query["showSecretKey"] == ["false"]
    assert requests[0][0].method == "GET"
    assert "secretAccessKey" not in result
    assert result["accessKeyId"] == "key-id"
