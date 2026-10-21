from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))

from ls_garage.reporting import ToolError
from ls_garage.validation import validate


def test_every_exposed_s3_operation_has_a_closed_schema() -> None:
    from ls_garage.storage_shapes import S3, args_schema
    for name in S3:
        schema = args_schema(name)
        for branch in schema.get("oneOf", [schema]):
            assert branch["additionalProperties"] is False
            assert set(branch["required"]) <= set(branch["properties"])


def test_update_bucket_clear_forms_and_ignored_values_are_explicit() -> None:
    validate("admin.UpdateBucket", {"id": "bucket", "corsRules": []})
    validate("admin.UpdateBucket", {"id": "bucket", "quotas": {"maxObjects": None, "maxSize": None}})
    validate("admin.UpdateBucket", {"id": "bucket", "websiteAccess": {"enabled": False}})
    with pytest.raises(ToolError, match="non-null"):
        validate("admin.UpdateBucket", {"id": "bucket", "corsRules": None})
    with pytest.raises(ToolError, match="explicit true"):
        validate("admin.AllowBucketKey", {"bucketId": "b", "accessKeyId": "k", "permissions": {"read": False}})


def test_import_key_has_only_protected_restore_interface() -> None:
    validate("admin.ImportKey", {"restore_existing": True, "restore_file": {"file": "/secure/key.json"}})
    with pytest.raises(ToolError):
        validate("admin.ImportKey", {"accessKeyId": "id", "secretAccessKey": "secret"})


def test_sse_c_post_requires_protected_output_and_flags() -> None:
    args = {"bucket": "bucket-a", "key": "x", "expires_seconds": 60, "max_content_length": 10,
            "encryption": {"algorithm": "SSE-C", "key_env": "KEY"}}
    with pytest.raises(ToolError, match="secret_output"):
        validate("s3.PresignPost", args)
    valid = {**args, "secret_output": "/secure/form.json"}
    assert validate("s3.PresignPost", valid)[:2] == ("s3", "PresignPost")


def test_config_requires_explicit_path_style_and_does_not_resolve_plan_references(tmp_path: Path) -> None:
    from ls_garage.config import load_config
    config = {"s3": {"endpoint": "https://garage.example", "region": "garage", "addressing_style": "path", "access_key_id": {"env": "UNSET"}, "secret_access_key": {"env": "UNSET2"}}}
    path = tmp_path / "config.json"; path.write_text(json.dumps(config))
    assert load_config(str(path))["s3"]["addressing_style"] == "path"
    config["s3"]["addressing_style"] = "virtual"; path.write_text(json.dumps(config))
    with pytest.raises(ToolError, match="addressing_style"):
        load_config(str(path))


def test_post_upload_rejects_unused_secret_output():
    with pytest.raises(ToolError):
        validate("s3.PostObject", {"bucket": "example.bucket", "key": "k", "source": "file", "secret_output": "must-not-be-ignored"})
