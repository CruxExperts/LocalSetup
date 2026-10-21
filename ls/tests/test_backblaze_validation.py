"""Validation/runtime-schema parity without optional dependencies."""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
from ls_backblaze import validation
from ls_backblaze.reporting import ToolError

def rejects(operation, args):
    with pytest.raises(ToolError): validation.validate(operation, args)

def test_sse_c_references_resume_and_part_limits() -> None:
    base={"bucket":"example.bucket","key":"k","source":"/x","checkpoint":"/c"}
    assert validation.validate("s3.ResumeMultipartUpload", {**base,"encryption":{"algorithm":"SSE-C","key":{"env":"KEY"}}})[2]
    assert validation.validate("s3.UploadFileMultipart", {**base,"part_size":5*1024*1024*1024})[2]
    rejects("s3.UploadFileMultipart", {**base,"part_size":5*1024*1024*1024+1})
    rejects("s3.ResumeMultipartUpload", {**base,"encryption":{"algorithm":"SSE-C","key":"raw"}})

def test_removed_and_closed_fields_match_registry() -> None:
    rejects("native.CreateBucket", {"bucket_name":"name","bucket_type":"allPrivate","default_retention":{}})
    rejects("s3.ListParts", {"bucket":"example.bucket","key":"k","upload_id":"u","encryption":"AES256"})
    schema=validation.request_schema()
    branches={b["properties"]["operation"]["const"]:set(b["properties"]["args"]["properties"]) for b in schema["oneOf"]}
    assert "encryption" in branches["s3.ResumeMultipartUpload"]
    assert "encryption" not in branches["s3.ListParts"]

@pytest.mark.parametrize(("operation","args"), [
 ("native.CreateKey", {"key_name":"***","capabilities":["listBuckets"],"secret_output":"/x"}),
 ("native.CreateKey", {"key_name":"key","capabilities":["unknown"],"secret_output":"/x"}),
 ("native.CreateKey", {"key_name":"key","capabilities":["listBuckets"],"secret_output":"/x","valid_duration_seconds":0}),
 ("native.ListKeys", {"max_key_count":10001}),
 ("s3.CompleteMultipartUpload", {"bucket":"example.bucket","key":"k","upload_id":"u","parts":[{"part_number":1,"etag":""}]}),
 ("s3.CompleteMultipartUpload", {"bucket":"example.bucket","key":"k","upload_id":"u","parts":[{"part_number":1,"etag":"a"},{"part_number":1,"etag":"b"}]}),
 ("s3.CopyObject", {"bucket":"example.bucket","key":"k","source_bucket":"BAD","source_key":"k"}),
 ("native.CreateBucket", {"bucket_name":"name","bucket_type":"invalid"}),
])
def test_reviewer_invalid_cases_reject(operation, args) -> None:
    rejects(operation,args)

@pytest.mark.parametrize("value", ["AES256", {"algorithm":"AES256"}, {"algorithm":"SSE-C","key_env":"KEY"}, {"algorithm":"SSE-C","key":{"file":"/protected/key"}}])
def test_all_supported_encryption_forms_are_schema_and_runtime_valid(value) -> None:
    args={"bucket":"example.bucket","key":"k","version_id":"v","encryption":value}
    assert validation.validate("s3.HeadObject",args)[2] == args
