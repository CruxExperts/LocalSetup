from __future__ import annotations

import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))

from ls_garage import admin


class _Response:
    def __init__(self, url: str, value: object): self.url, self.value = url, io.BytesIO(json.dumps(value).encode())
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def geturl(self): return self.url
    def read(self, *args): return self.value.read(*args)


class _Opener:
    def __init__(self, value: object): self.value = value; self.request = None
    def open(self, request, timeout): self.request = request; return _Response(request.full_url, self.value)


def test_get_key_info_forces_show_secret_false_and_projects_secret(monkeypatch) -> None:
    opener = _Opener({"accessKeyId": "id", "name": "n", "expired": False, "permissions": {}, "buckets": [], "secretAccessKey": "secret", "unknown": "drop"})
    monkeypatch.setattr(admin, "build_opener", lambda *args: opener)
    result = admin.execute("GetKeyInfo", {"id": "id"}, {"endpoint": "https://garage.example", "token": "token", "request_budget_seconds": 20})
    assert result == {"accessKeyId": "id", "name": "n", "expired": False, "permissions": {}, "buckets": []}
    assert "showSecretKey=false" in opener.request.full_url
    assert opener.request.get_header("Authorization") == "Bearer token"


def test_admin_write_is_not_retried_after_unconfirmed_delivery(monkeypatch) -> None:
    calls = 0
    class Broken:
        def open(self, request, timeout):
            nonlocal calls; calls += 1; raise OSError("lost")
    monkeypatch.setattr(admin, "build_opener", lambda *args: Broken())
    try:
        admin.execute("DeleteBucket", {"id": "bucket"}, {"endpoint": "https://garage.example", "token": "token", "request_budget_seconds": 20})
    except Exception as error:
        assert error.code == "transport_failure" and error.exit_name == "uncertain"
    assert calls == 1
