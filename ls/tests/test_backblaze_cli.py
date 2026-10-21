"""Offline contract tests for the independent Backblaze optional skill."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/backblaze.py"


def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", "-S", str(CLI), *args], input=input_text, text=True, capture_output=True, check=False, cwd="/")


def test_schema_is_real_versioned_json_schema_without_sdk() -> None:
    request = run("--schema", "request")
    result = run("--schema", "result")
    assert request.returncode == result.returncode == 0
    request_value, result_value = json.loads(request.stdout), json.loads(result.stdout)
    assert request_value["$schema"].endswith("2020-12/schema")
    assert any(branch["properties"]["operation"].get("const") == "s3.ListObjectsV2" for branch in request_value["oneOf"])
    assert result_value["properties"]["outcome"]["enum"] == ["planned", "succeeded", "partial", "failed", "unknown"]


def test_plan_is_offline_and_closed_fields_are_rejected() -> None:
    planned = run("--tool", "s3.ListObjectsV2", "--args-json", '{"bucket":"example.bucket","prefix":"logs/"}')
    assert planned.returncode == 0
    assert json.loads(planned.stdout)["outcome"] == "planned"
    rejected = run("--tool", "s3.ListObjectsV2", "--args-json", '{"bucket":"example.bucket","unexpected":true}')
    assert rejected.returncode == 2
    assert json.loads(rejected.stdout)["error"]["code"] == "unknown_field"


def test_mutation_flags_and_version_delete_meaning() -> None:
    blocked = run("--tool", "s3.PutObject", "--args-json", '{"bucket":"example.bucket","key":"x","source":"/x"}')
    assert blocked.returncode == 3
    assert json.loads(blocked.stdout)["error"]["code"] == "overwrite_not_allowed"
    marker = run("--tool", "s3.DeleteObject", "--allow-destructive", "--args-json", '{"bucket":"example.bucket","key":"x"}')
    assert marker.returncode == 0
    assert json.loads(marker.stdout)["outcome"] == "planned"
    permanent = run("--tool", "s3.DeleteObject", "--args-json", '{"bucket":"example.bucket","key":"x","version_id":"opaque"}')
    assert permanent.returncode == 3


def test_missing_sdk_is_structured_only_when_s3_apply_reached(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"s3": {"endpoint": "https://s3.us-west-004.backblazeb2.com", "region": "us-west-004", "access_key_id": {"env": "NO_SUCH_ID"}, "secret_access_key": {"env": "NO_SUCH_SECRET"}}}))
    completed = run("--tool", "s3.ListBuckets", "--args-json", "{}", "--config", str(config), "--apply")
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "credential_unavailable"


def test_notification_raw_secret_and_insecure_endpoint_reject_locally(tmp_path: Path) -> None:
    secret = tmp_path / "secret"; secret.write_text("x"); secret.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"native": {"endpoint": "https://evilbackblaze.com", "key_id": {"file": str(secret)}, "application_key": {"file": str(secret)}}}))
    endpoint = run("--tool", "native.ListBuckets", "--args-json", "{}", "--config", str(config), "--apply")
    assert endpoint.returncode == 2
    raw = run("--tool", "native.SetNotificationRules", "--allow-access-change", "--args-json", '{"bucket_id":"b","rules":[{"authorization":"do-not-pass"}]}')
    assert raw.returncode == 2
    assert json.loads(raw.stdout)["error"]["code"] == "invalid_argument"
