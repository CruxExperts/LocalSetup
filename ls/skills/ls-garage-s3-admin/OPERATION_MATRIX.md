# Garage operation matrix

This skill is pinned to a Garage Admin API v2.3.0 projection. Its original
official source SHA-256 is
`07ffb2d0febbf386747eed0232b1a97931d323182f11e77537519620e6eb10d8`, and the
official Garage S3 compatibility page reviewed 2026-09-08. `Read` operations
may retry at most three sends. `Write` operations send once and report an
unknown outcome if delivery is not confirmed. Every row has an offline schema
and capability test; transport tests use botocore stubs or local fixtures.

| API/capability | Operations | Class | Acceptance boundary |
| --- | --- | --- | --- |
| Admin discovery | `ListBuckets`, `GetBucketInfo`, `ListKeys`, `GetKeyInfo`, `GetClusterHealth` | Read | HTTPS bearer request, allowlisted path, closed projected response; `GetKeyInfo` fixes `showSecretKey=false`. |
| Admin bucket control | `CreateBucket`, `DeleteBucket`, `UpdateBucket`, `AllowBucketKey`, `DenyBucketKey`, `AddBucketAlias`, `RemoveBucketAlias` | Write/access/destructive | Explicit change flags; outer null update rejected; empty CORS/lifecycle clears; quota/website clear forms preserved. |
| Admin key control | `CreateKey`, `UpdateKey`, `ImportKey`, `DeleteKey` | Write/access/destructive | Protected key delivery/export; import requires restore intent and matching existing ID/name/secret. |
| S3 bucket/object reads | `ListBuckets`, `HeadBucket`, `GetBucketLocation`, `ListObjects`, `ListObjectsV2`, `HeadObject`, `GetObject` | Read | Bounded pagination/streaming download; no-clobber local publication and SSE-C headers. |
| S3 bucket/object writes | `CreateBucket`, `DeleteBucket`, `PutObject`, `PostObject`, `CopyObject`, `DeleteObject`, `DeleteObjects` | Write | Required destructive/overwrite flags; Garage-only inputs; responses are projected and unconfirmed delivery is unknown. |
| Multipart | `CreateMultipartUpload`, `UploadPart`, `UploadPartCopy`, `ListParts`, `ListMultipartUploads`, `CompleteMultipartUpload`, `AbortMultipartUpload`, `UploadFileMultipart`, `ResumeMultipartUpload` | Read/write | 10,000-part bound, locked fsync checkpoint, remote reconciliation, SSE-C creation/part/list/complete headers. |
| S3 web configuration | `GetBucketCors`, `PutBucketCors`, `DeleteBucketCors`, `GetBucketWebsite`, `PutBucketWebsite`, `DeleteBucketWebsite`, `GetBucketLifecycleConfiguration`, `PutBucketLifecycleConfiguration`, `DeleteBucketLifecycle` | Read/access/write | CORS and documented lifecycle/website subset only; redirects, version actions and unsupported lifecycle fields reject locally. |
| Presigning | `PresignGet`, `PresignPut`, `PresignPost` | Read / overwrite | SigV4 GET/PUT URL or POST form. URLs/forms are sensitive; every SSE-C presign delivers the complete URL and headers/form only to a protected output file. |

The unavailable S3 capability set is deliberately rejected: versioning and
version recovery, conditional no-clobber, ACLs/policies, default/server-managed
encryption, Object Lock, retention, legal hold, replication, notifications,
and unsupported `x-amz-*` controls. No live Garage provider calls are part of
the test suite.

## Exact operation contract

Every S3 row uses the closed request schema emitted by `--schema request` and
the generated botocore result contract (except `PostObject`, which has its
closed streaming result). Every Admin row uses the extracted v2.3.0 schema.
`R3` means at most three safe-read attempts; `W1` means one mutation send and
an unknown outcome on an unconfirmed response. `D`, `A`, `P`, and `O` require
the destructive, access-change, public, and overwrite flags respectively.
`Local` means SDK signing without a provider request; encrypted signing still
requires exclusive local secret delivery. `test_garage_matrix.py` covers every
row's valid/invalid requests and permission gates, all Admin methods/bodies and
success/error results, and every direct S3 method through the real locked SDK.
`test_garage_post_security.py`, `test_garage_reliability.py`, and
`test_garage_transfer_recovery.py` cover signing, HTTP POST, secrets, retries,
streaming, and recovery. Materialized imports are covered by
`test_storage_skill_installation.py`; these are offline tests.

Server permissions below come from the tagged
[authorization router](https://github.com/deuxfleurs-org/garage/blob/7b119c0b4fa58ab3cb6d5db435fe52d990f6a7aa/src/api/s3/router.rs#L579),
[signature/dispatch boundary](https://github.com/deuxfleurs-org/garage/blob/7b119c0b4fa58ab3cb6d5db435fe52d990f6a7aa/src/api/s3/api_server.rs#L126),
and [bucket creation handler](https://github.com/deuxfleurs-org/garage/blob/7b119c0b4fa58ab3cb6d5db435fe52d990f6a7aa/src/api/s3/bucket.rs#L210).
All S3 requests require a valid signature or signed form; no bucket permission
for `ListBuckets` does not mean anonymous access. The server evaluates actual
permissions; a local plan does not claim that credentials possess them.

| Namespace operation | API / method | Server capability | Gate, retry | Contract and fixture |
| --- | --- | --- | --- | --- |
| `s3.ListBuckets` | S3 `ListBuckets` | Authenticated key; no bucket check | R3 | Closed list result; SDK stub. |
| `s3.HeadBucket` | S3 `HeadBucket` | Bucket read | R3 | Path-style bucket probe; schema fixture. |
| `s3.GetBucketLocation` | S3 `GetBucketLocation` | Bucket read | R3 | Closed location result; schema fixture. |
| `s3.CreateBucket` | S3 `CreateBucket` | Key `allow_create_bucket` | W1,A | Mutation identity result; schema fixture. |
| `s3.DeleteBucket` | S3 `DeleteBucket` | Bucket owner | W1,D,A | Explicit destructive deletion; schema fixture. |
| `s3.ListObjects` | S3 `ListObjects` | Bucket read | R3 | Bounded marker pagination; SDK/result fixture. |
| `s3.ListObjectsV2` | S3 `ListObjectsV2` | Bucket read | R3 | Bounded continuation pagination; SDK/result fixture. |
| `s3.HeadObject` | S3 `HeadObject` | Bucket read | R3 | SSE-C header request; SDK fixture. |
| `s3.GetObject` | S3 `GetObject` | Bucket read | R3 | Streaming destination no-clobber; transfer fixture. |
| `s3.PutObject` | S3 `PutObject` | Bucket write | W1,O | Streamed 5-GiB-bounded source; SDK fixture. |
| `s3.PostObject` | S3 `PostObject` | Bucket write | W1,O | SigV4 streaming multipart form; schema fixture. |
| `s3.CopyObject` | S3 `CopyObject` | Destination write + source read/decrypt | W1,O | Explicit copy source and SSE-C; schema fixture. |
| `s3.DeleteObject` | S3 `DeleteObject` | Bucket write | W1,D | Explicit destructive deletion; schema fixture. |
| `s3.DeleteObjects` | S3 `DeleteObjects` | Bucket write | W1,D | Member result/partial result; schema fixture. |
| `s3.CreateMultipartUpload` | S3 multipart create | Bucket write | W1,O | SSE-C headers and upload ID; SDK fixture. |
| `s3.UploadPart` | S3 multipart part | Bucket write | W1 | SSE-C part header and ETag; SDK fixture. |
| `s3.UploadPartCopy` | S3 multipart copy part | Destination write + source read/decrypt | W1 | Source/destination SSE-C headers; schema fixture. |
| `s3.ListParts` | S3 `ListParts` | Bucket read | R3 | SSE-C list header and pagination; SDK fixture. |
| `s3.ListMultipartUploads` | S3 upload list | Bucket read | R3 | Closed upload-list result; schema fixture. |
| `s3.CompleteMultipartUpload` | S3 multipart complete | Bucket write | W1,O | Ordered parts and SSE-C header; SDK fixture. |
| `s3.AbortMultipartUpload` | S3 multipart abort | Bucket write | W1,D | Explicit destructive abort; schema fixture. |
| `s3.UploadFileMultipart` | high-level multipart | Bucket write | W1,O | Exclusive fsync checkpoint/recovery; transfer fixture. |
| `s3.ResumeMultipartUpload` | high-level multipart | Bucket read + write | W1,O | Read reconciliation before continuing; transfer fixture. |
| `s3.PresignGet` | SigV4 GET query | Eventual bucket read | Local | Sensitive URL with bounded expiry; schema fixture. |
| `s3.PresignPut` | SigV4 PUT query | Eventual bucket write | Local,O | Sensitive URL with bounded expiry; schema fixture. |
| `s3.PresignPost` | SigV4 POST policy | Eventual bucket write | Local,O | SSE-C raw key only in protected output; SDK fixture. |
| `s3.GetBucketCors` | S3 CORS GET | Bucket owner | R3 | Closed CORS response; schema fixture. |
| `s3.PutBucketCors` | S3 CORS PUT | Bucket owner | W1,A | Garage CORS subset; schema fixture. |
| `s3.DeleteBucketCors` | S3 CORS delete | Bucket owner | W1,A | Explicit access change; schema fixture. |
| `s3.GetBucketWebsite` | S3 website GET | Bucket owner | R3 | Closed website response; schema fixture. |
| `s3.PutBucketWebsite` | S3 website PUT | Bucket owner | W1,A,P | Index/error subset only; schema fixture. |
| `s3.DeleteBucketWebsite` | S3 website delete | Bucket owner | W1,A | Explicit access change; schema fixture. |
| `s3.GetBucketLifecycleConfiguration` | S3 lifecycle GET | Bucket read | R3 | Closed lifecycle response; schema fixture. |
| `s3.PutBucketLifecycleConfiguration` | S3 lifecycle PUT | Bucket write | W1,A | Expiration/abort-only subset; validation fixture. |
| `s3.DeleteBucketLifecycle` | S3 lifecycle delete | Bucket write | W1,A | Explicit access change; schema fixture. |
| `admin.ListBuckets` | `GET /v2/ListBuckets` | Explicit Admin bearer token | R3 | Allowlisted projected list; admin fixture. |
| `admin.GetBucketInfo` | `GET /v2/GetBucketInfo` | Explicit Admin bearer token | R3 | Exactly one lookup selector; validation fixture. |
| `admin.CreateBucket` | `POST /v2/CreateBucket` | Explicit Admin bearer token | W1,A | Closed bucket result; schema fixture. |
| `admin.DeleteBucket` | `POST /v2/DeleteBucket` | Explicit Admin bearer token | W1,D,A | Required `id`; schema fixture. |
| `admin.UpdateBucket` | `POST /v2/UpdateBucket` | Explicit Admin bearer token | W1,A | Clear semantics and outer-null rejection; validation fixture. |
| `admin.ListKeys` | `GET /v2/ListKeys` | Explicit Admin bearer token | R3 | Projected key list without secrets; schema fixture. |
| `admin.GetKeyInfo` | `GET /v2/GetKeyInfo` | Explicit Admin bearer token | R3 | Forced `showSecretKey=false`; admin fixture. |
| `admin.CreateKey` | `POST /v2/CreateKey` | Explicit Admin bearer token | W1,A | 0600 fsync secret delivery; secret fixture. |
| `admin.UpdateKey` | `POST /v2/UpdateKey` | Explicit Admin bearer token | W1,A | Explicit true permission changes; validation fixture. |
| `admin.ImportKey` | `POST /v2/ImportKey` | Explicit Admin bearer token | W1,A | Existing protected restore only; secret fixture. |
| `admin.DeleteKey` | `POST /v2/DeleteKey` | Explicit Admin bearer token | W1,D,A | Required explicit destructive flag; schema fixture. |
| `admin.AllowBucketKey` | `POST /v2/AllowBucketKey` | Explicit Admin bearer token | W1,A | Only true permission flags; validation fixture. |
| `admin.DenyBucketKey` | `POST /v2/DenyBucketKey` | Explicit Admin bearer token | W1,A | Only true permission flags; validation fixture. |
| `admin.AddBucketAlias` | `POST /v2/AddBucketAlias` | Explicit Admin bearer token | W1,A | Exclusive global/local alias shape; schema fixture. |
| `admin.RemoveBucketAlias` | `POST /v2/RemoveBucketAlias` | Explicit Admin bearer token | W1,A | Exclusive global/local alias shape; schema fixture. |
| `admin.GetClusterHealth` | `GET /v2/GetClusterHealth` | Explicit Admin bearer token | R3 | Closed health result; schema fixture. |
