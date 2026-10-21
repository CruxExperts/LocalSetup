"""Public schemas must describe requests the offline runtime can actually use."""
from __future__ import annotations

import importlib
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_SKILLS = {
    "backblaze": "ls-backblaze-b2-storage",
    "garage": "ls-garage-s3-admin",
}


@pytest.fixture(params=("backblaze",))
def validation(request, monkeypatch):
    monkeypatch.syspath_prepend(
        str(ROOT / "ls/skills" / PROVIDER_SKILLS[request.param] / "scripts/lib")
    )
    return importlib.import_module(f"ls_{request.param}.validation")


def test_closed_operation_branches_have_no_impossible_required_fields(validation):
    def inspect(schema):
        if isinstance(schema, dict):
            if schema.get("additionalProperties") is False:
                assert set(schema.get("required", [])) <= set(schema.get("properties", {})), schema
            for value in schema.values():
                inspect(value)
        elif isinstance(schema, list):
            for value in schema:
                inspect(value)
    inspect(validation.request_schema())


@pytest.mark.parametrize(("operation", "args"), [
    ("s3.CreateBucket", {"bucket": "example-bucket"}),
    ("s3.DeleteBucket", {"bucket": "example-bucket"}),
    ("s3.GetObject", {"bucket": "example-bucket", "key": "a/../x%2Fλ", "destination": "download"}),
    ("s3.ListObjectsV2", {"bucket": "example-bucket", "prefix": "logs/", "max_keys": 100}),
])
def test_s3_requests_are_accepted_by_both_schema_and_runtime(validation, operation, args):
    validation.validate(operation, args)
    jsonschema.Draft202012Validator(validation.request_schema()).validate({"operation": operation, "args": args})


@pytest.mark.parametrize("unsupported", [
    {"key": "not-a-list-parameter"}, {"range": "bytes=0-4"},
    {"version_id": "opaque-version"}, {"max_keys": True},
])
def test_invalid_list_arguments_are_rejected_by_schema_and_runtime(validation, unsupported):
    args = {"bucket": "example-bucket", **unsupported}
    with pytest.raises(Exception) as captured:
        validation.validate("s3.ListObjectsV2", args)
    assert type(captured.value).__name__ == "ToolError"
    assert not jsonschema.Draft202012Validator(validation.request_schema()).is_valid(
        {"operation": "s3.ListObjectsV2", "args": args}
    )
