"""The documented Garage S3 subset; unsupported AWS options reject locally."""
from __future__ import annotations

from typing import Any


def text(minimum=1, maximum=4096):
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def integer(minimum=0, maximum=2**63-1):
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def obj(properties, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}


def array(items, minimum=0, maximum=10000):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


BUCKET = {**text(3, 63), "pattern": r"^(?!.*\.\.)(?!\d+\.\d+\.\d+\.\d+$)[a-z0-9][a-z0-9.-]*[a-z0-9]$"}
REF = {"oneOf": [obj({"env": text()}, ("env",)), obj({"file": text(), "field": text()}, ("file",))]}
SSE = {"oneOf": [obj({"algorithm": {"const": "SSE-C"}, "key": REF}, ("algorithm", "key")), obj({"algorithm": {"const": "SSE-C"}, "key_env": text()}, ("algorithm", "key_env"))]}
CORS = obj({"AllowedOrigins": array(text(), 1), "AllowedMethods": array({"enum": ["GET", "HEAD", "PUT", "POST", "DELETE"]}, 1), "AllowedHeaders": array(text()), "ExposeHeaders": array(text()), "ID": text(), "MaxAgeSeconds": integer()}, ("AllowedOrigins", "AllowedMethods"))
AND = {**obj({"Prefix": text(0), "ObjectSizeGreaterThan": integer(), "ObjectSizeLessThan": integer()}), "minProperties": 2}
FILTER = {**obj({"Prefix": text(0), "ObjectSizeGreaterThan": integer(), "ObjectSizeLessThan": integer(), "And": AND}), "maxProperties": 1}
EXPIRATION = {"oneOf": [obj({"Days": integer(1)}, ("Days",)), obj({"Date": {**text(), "format": "date-time"}}, ("Date",))]}
LIFECYCLE = obj({"ID": text(), "Status": {"enum": ["Enabled", "Disabled"]}, "Filter": FILTER, "Expiration": EXPIRATION, "AbortIncompleteMultipartUpload": obj({"DaysAfterInitiation": integer(1)}, ("DaysAfterInitiation",))}, ("Status",))
LIFECYCLE["anyOf"] = [{"required": ["Expiration"]}, {"required": ["AbortIncompleteMultipartUpload"]}]
WEBSITE = obj({"IndexDocument": obj({"Suffix": text()}, ("Suffix",)), "ErrorDocument": obj({"Key": text()}, ("Key",))}, ("IndexDocument",))

S3 = {
    "ListBuckets": set(), "HeadBucket": {"bucket"}, "GetBucketLocation": {"bucket"}, "CreateBucket": {"bucket"}, "DeleteBucket": {"bucket"},
    "ListObjects": {"bucket", "prefix", "delimiter", "max_keys", "marker", "encoding_type", "max_pages"},
    "ListObjectsV2": {"bucket", "prefix", "delimiter", "max_keys", "continuation_token", "start_after", "encoding_type", "fetch_owner", "max_pages"},
    "HeadObject": {"bucket", "key", "encryption"}, "GetObject": {"bucket", "key", "destination", "range", "overwrite", "encryption"},
    "PutObject": {"bucket", "key", "source", "metadata", "content_type", "content_encoding", "encryption"},
    "PostObject": {"bucket", "key", "source", "content_type", "encryption"},
    "CopyObject": {"bucket", "key", "source_bucket", "source_key", "metadata_directive", "metadata", "encryption", "source_encryption"},
    "DeleteObject": {"bucket", "key"}, "DeleteObjects": {"bucket", "objects", "quiet"},
    "CreateMultipartUpload": {"bucket", "key", "content_type", "metadata", "encryption"},
    "UploadPart": {"bucket", "key", "upload_id", "part_number", "source", "encryption"},
    "UploadPartCopy": {"bucket", "key", "upload_id", "part_number", "source_bucket", "source_key", "source_range", "encryption", "source_encryption"},
    "ListParts": {"bucket", "key", "upload_id", "max_parts", "part_number_marker", "encryption"},
    "ListMultipartUploads": {"bucket", "prefix", "delimiter", "key_marker", "upload_id_marker", "max_uploads"},
    "CompleteMultipartUpload": {"bucket", "key", "upload_id", "parts", "encryption"}, "AbortMultipartUpload": {"bucket", "key", "upload_id"},
    "UploadFileMultipart": {"bucket", "key", "source", "checkpoint", "part_size", "content_type", "metadata", "encryption"},
    "ResumeMultipartUpload": {"bucket", "key", "source", "checkpoint", "encryption"},
    "PresignGet": {"bucket", "key", "expires_seconds", "encryption", "secret_output"},
    "PresignPut": {"bucket", "key", "expires_seconds", "content_type", "encryption", "secret_output"},
    "PresignPost": {"bucket", "key", "expires_seconds", "content_type", "min_content_length", "max_content_length", "encryption", "secret_output"},
    **{name: {"bucket"} for name in ("GetBucketCors", "DeleteBucketCors", "GetBucketWebsite", "DeleteBucketWebsite", "GetBucketLifecycleConfiguration", "DeleteBucketLifecycle")},
    "PutBucketCors": {"bucket", "cors_rules"}, "PutBucketWebsite": {"bucket", "website"}, "PutBucketLifecycleConfiguration": {"bucket", "rules"},
}
REQUIRED = {"GetObject": {"destination"}, "PutObject": {"source"}, "PostObject": {"source"}, "CopyObject": {"source_bucket", "source_key"}, "DeleteObjects": {"objects"}, "UploadPart": {"source", "part_number", "upload_id"}, "UploadPartCopy": {"source_bucket", "source_key", "part_number", "upload_id"}, "ListParts": {"upload_id"}, "CompleteMultipartUpload": {"upload_id", "parts"}, "AbortMultipartUpload": {"upload_id"}, "UploadFileMultipart": {"source", "checkpoint"}, "ResumeMultipartUpload": {"source", "checkpoint"}, "PresignGet": {"expires_seconds"}, "PresignPut": {"expires_seconds"}, "PresignPost": {"expires_seconds", "max_content_length"}, "PutBucketCors": {"cors_rules"}, "PutBucketWebsite": {"website"}, "PutBucketLifecycleConfiguration": {"rules"}}


def field(name):
    numbers = {"max_keys": (0,1000), "max_pages": (1,1000), "max_parts": (1,1000), "max_uploads": (1,1000), "part_number": (1,10000), "part_number_marker": (0,10000), "part_size": (5*1024**2,5*1024**3), "expires_seconds": (1,604800), "min_content_length": (0,5*1024**3), "max_content_length": (0,5*1024**3)}
    if name in numbers:
        return integer(*numbers[name])
    if name in {"overwrite", "quiet", "fetch_owner"}:
        return {"type": "boolean"}
    if name in {"bucket", "source_bucket"}:
        return BUCKET
    if name in {"encryption", "source_encryption"}:
        return SSE
    shapes = {"key": text(1,1024), "source_key": text(1,1024), "prefix": text(0), "delimiter": text(0), "start_after": text(0), "metadata": {"type":"object","maxProperties":50,"propertyNames":text(1,128),"additionalProperties":text(0,2048)}, "metadata_directive":{"enum":["COPY","REPLACE"]}, "encoding_type":{"const":"url"}, "objects":array(obj({"key":text(1,1024)},("key",)),1,1000), "parts":array(obj({"part_number":integer(1,10000),"etag":text()},("part_number","etag")),1), "cors_rules":array(CORS,1,100), "website":WEBSITE, "rules":array(LIFECYCLE,1,1000), "range":{**text(),"pattern":r"^bytes=(?:\d+-\d*|-\d+)$"}, "source_range":{**text(),"pattern":r"^bytes=\d+-\d+$"}}
    return shapes.get(name,text())


def args_schema(name):
    fields = S3[name]
    required = REQUIRED.get(name,set()) | ({"bucket"} if "bucket" in fields else set()) | ({"key"} if "key" in fields else set())
    properties = {item:field(item) for item in sorted(fields)}
    if name in {"PresignGet", "PresignPut", "PresignPost"}:
        return {"oneOf": [obj({key:value for key,value in properties.items() if key not in {"encryption", "secret_output"}}, sorted(required)),
                          obj(properties, sorted(required | {"encryption", "secret_output"}))]}
    return obj(properties, sorted(required))
