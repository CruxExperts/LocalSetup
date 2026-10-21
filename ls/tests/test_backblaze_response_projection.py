"""Adversarial responses cannot leak arbitrary headers or undeclared fields."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import native, s3, validation
from ls_backblaze.reporting import envelope
from ls.core.s3_sdk.responses import update


def test_sdk_response_drops_raw_headers_and_unknown_nested_fields():
    raw = {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {"x-unexpected-token": "top-secret"}, "unexpectedToken": "top-secret"}, "unexpectedToken": "top-secret", "CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"], "unexpectedToken": "top-secret"}]}
    result = s3._response("GetBucketCors", raw)
    assert result == {"ResponseMetadata": {"HTTPStatusCode": 200}, "CORSRules": [{"AllowedMethods": ["GET"], "AllowedOrigins": ["*"]}]}
    message = envelope("s3.GetBucketCors", outcome="succeeded", result=result)
    assert "top-secret" not in json.dumps(message)
    Draft202012Validator(validation.result_schema()).validate(message)
    message["result"]["unexpectedToken"] = "top-secret"
    assert not Draft202012Validator(validation.result_schema()).is_valid(message)


def test_native_key_lists_and_notifications_drop_undocumented_secrets():
    listed = native._validate_result("ListKeys", {"keys": [{"applicationKeyId": "id", "applicationKey": "top-secret", "unknownToken": "top-secret"}], "unknownToken": "top-secret"})
    assert listed == {"keys": [{"applicationKeyId": "id"}]}
    rules = native._validate_result("GetNotificationRules", [{"bucketId": "id", "eventNotificationRules": [{"name": "rule", "targetConfiguration": {"targetType": "webhook", "url": "https://example.invalid", "customHeaders": [{"name": "Authorization", "value": "top-secret"}], "hmacSha256SigningSecret": "top-secret"}}]}])
    assert "top-secret" not in json.dumps(rules)
    Draft202012Validator(validation.result_schema()).validate(envelope("native.GetNotificationRules", outcome="succeeded", result=rules))


def test_timestamps_are_normalized_before_typed_result_emission():
    result = s3._response("HeadObject", {"ResponseMetadata": {"HTTPStatusCode": 200}, "ContentLength": 0, "LastModified": datetime(2026, 9, 8, tzinfo=timezone.utc)})
    assert result["LastModified"] == "2026-09-08T00:00:00+00:00"
    Draft202012Validator(validation.result_schema()).validate(envelope("s3.HeadObject", outcome="succeeded", result=result))


def test_generated_locked_sdk_response_contract_has_no_drift():
    update(ROOT, check=True)
