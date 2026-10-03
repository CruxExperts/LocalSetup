# Backblaze B2 operation matrix (schema version 1)

The closed request schema from `--schema request` is the authoritative nested
field/type/limit contract; `--schema result` defines typed operation results.
The table lists every executable operation and its required (`*`) and optional
fields. Each operation maps to its provider API by the same name, except the
explicit transfer/presigning workflows described below. Unknown fields reject
locally; AWS SDK support alone never expands this allowlist.

Sources: [S3 API overview](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api),
[native API reference](https://www.backblaze.com/apidocs/),
[notifications](https://www.backblaze.com/apidocs/b2-set-bucket-notification-rules),
[regional endpoint and application-key guide](https://www.backblaze.com/docs/en/cloud-storage-get-started-with-a-backblaze-integration),
[AWS CLI presign example](https://www.backblaze.com/docs/cloud-storage-use-the-aws-cli-with-backblaze-b2),
[event notification limits](https://www.backblaze.com/docs/cloud-storage-event-notifications-reference-guide),
[S3 lifecycle support and limits](https://www.backblaze.com/apidocs/s3-put-lifecycle-configuration),
[lifecycle API ownership warning](https://www.backblaze.com/apidocs/b2-update-bucket),
[default encryption behavior](https://www.backblaze.com/blog/backblaze-b2-to-encrypt-new-uploads-by-default/),
[IAM/STS availability](https://www.backblaze.com/docs/cloud-storage-iam-sts-api),
reviewed 2026-10-02. Native calls use `/b2api/v4/`; account authorization and
notification retrieval use GET, other native calls use POST. Notification GET
uses the `bucketId` query parameter; notification updates send an array. A
bucket-restricted application key also needs `listAllBucketNames` for S3
`ListBuckets`.

`R` means at most three attempts per safe network request; a native invocation
shares a nine-send ceiling across initial authorization, the selected operation,
and one refresh authorization. `W` means one attempt per mutation, with unknown
outcomes requiring read-only reconciliation. Presigning is local after explicit
configuration. SDK retries and region redirect replay are disabled. The local
S3 configuration currently requires the endpoint region and signing region to
match; Backblaze's published presign example uses a different signing scope, so
this is a local restriction and not established as universal provider behavior.
Reads within multipart resume use the same bounded policy; the workflow itself
is never replayed.

| Operation / API | Request fields (`*` required) | B2 capability | Execution class |
|---|---|---|---|
| `s3.ListBuckets` | `{}` | listBuckets; restricted keys also need listAllBucketNames | R |
| `s3.HeadBucket` | `bucket*` | listBuckets | R |
| `s3.GetBucketLocation` | `bucket*` | listBuckets | R |
| `s3.CreateBucket` | `bucket*`, `object_lock_enabled` | writeBuckets | W |
| `s3.DeleteBucket` | `bucket*` | deleteBuckets | W; destructive; access change |
| `s3.GetBucketAcl` | `bucket*` | listBuckets | R |
| `s3.GetBucketCors` | `bucket*` | listBuckets | R |
| `s3.GetBucketEncryption` | `bucket*` | readBucketEncryption | R |
| `s3.GetBucketLogging` | `bucket*` | readBucketLogging | R |
| `s3.GetBucketVersioning` | `bucket*` | listBuckets | R |
| `s3.PutBucketAcl` | `acl*`, `bucket*` | writeBuckets | W; access change |
| `s3.PutBucketCors` | `bucket*`, `cors_rules*` | writeBuckets | W; access change |
| `s3.DeleteBucketCors` | `bucket*` | writeBuckets | W; access change |
| `s3.PutBucketEncryption` | `bucket*`, `encryption*` | writeBucketEncryption | W; access change |
| `s3.DeleteBucketEncryption` | `bucket*` | writeBucketEncryption | W; access change |
| `s3.PutBucketLogging` | `bucket*`, `logging*` | writeBucketLogging | W; access change |
| `s3.ListObjects` | `bucket*`, `delimiter`, `encoding_type`, `marker`, `max_keys`, `max_pages`, `prefix` | listFiles | R |
| `s3.ListObjectsV2` | `bucket*`, `continuation_token`, `delimiter`, `encoding_type`, `fetch_owner`, `max_keys`, `max_pages`, `prefix`, `start_after` | listFiles | R |
| `s3.ListObjectVersions` | `bucket*`, `delimiter`, `encoding_type`, `key_marker`, `max_keys`, `max_pages`, `prefix`, `version_id_marker` | listFiles | R |
| `s3.HeadObject` | `bucket*`, `encryption`, `key*`, `version_id` | readFiles | R |
| `s3.GetObject` | `bucket*`, `destination*`, `encryption`, `include_recovery_path`, `key*`, `overwrite`, `range`, `version_id` | readFiles | R |
| `s3.PutObject` | `bucket*`, `content_encoding`, `content_type`, `encryption`, `key*`, `metadata`, `source*` | writeFiles | W; overwrite |
| `s3.CopyObject` | `bucket*`, `encryption`, `key*`, `metadata`, `metadata_directive`, `source_bucket*`, `source_encryption`, `source_key*`, `source_version_id` | readFiles + writeFiles | W; overwrite |
| `s3.DeleteObject` | `bucket*`, `key*`, `version_id` | deleteFiles | W; destructive |
| `s3.DeleteObjects` | `bucket*`, `objects*`, `quiet` | deleteFiles | W; destructive |
| `s3.GetObjectAcl` | `bucket*`, `key*`, `version_id` | readFiles | R |
| `s3.GetObjectTagging` | `bucket*`, `key*`, `version_id` | readFiles | R |
| `s3.PutObjectAcl` | `acl*`, `bucket*`, `key*`, `version_id` | writeFiles | W; access change |
| `s3.GetObjectLegalHold` | `bucket*`, `key*`, `version_id` | readFileLegalHolds | R |
| `s3.PutObjectLegalHold` | `bucket*`, `key*`, `status*`, `version_id` | writeFileLegalHolds | W; access change |
| `s3.GetObjectRetention` | `bucket*`, `key*`, `version_id` | readFileRetentions | R |
| `s3.PutObjectRetention` | `bucket*`, `bypass_governance`, `key*`, `retention*`, `version_id` | writeFileRetentions | W; access change |
| `s3.GetObjectLockConfiguration` | `bucket*` | readBucketRetentions | R |
| `s3.PutObjectLockConfiguration` | `bucket*`, `configuration*` | writeBucketRetentions | W; access change |
| `s3.CreateMultipartUpload` | `bucket*`, `content_type`, `encryption`, `key*`, `metadata` | writeFiles | W |
| `s3.UploadPart` | `bucket*`, `encryption`, `key*`, `part_number*`, `source*`, `upload_id*` | writeFiles | W |
| `s3.UploadPartCopy` | `bucket*`, `encryption`, `key*`, `part_number*`, `source_bucket*`, `source_encryption`, `source_key*`, `source_range`, `upload_id*` | readFiles + writeFiles | W |
| `s3.ListParts` | `bucket*`, `key*`, `max_parts`, `part_number_marker`, `upload_id*` | listFiles | R |
| `s3.ListMultipartUploads` | `bucket*`, `delimiter`, `key_marker`, `max_uploads`, `prefix`, `upload_id_marker` | listFiles | R |
| `s3.CompleteMultipartUpload` | `bucket*`, `key*`, `parts*`, `upload_id*` | writeFiles | W; overwrite |
| `s3.AbortMultipartUpload` | `bucket*`, `key*`, `upload_id*` | writeFiles | W; destructive |
| `s3.UploadFileMultipart` | `bucket*`, `checkpoint*`, `content_type`, `encryption`, `key*`, `metadata`, `part_size`, `source*` | writeFiles | W; overwrite |
| `s3.ResumeMultipartUpload` | `bucket*`, `checkpoint*`, `encryption`, `key*`, `source*` | writeFiles | W; overwrite |
| `s3.PresignGet` | `bucket*`, `encryption`, `expires_seconds*`, `key*`, `version_id` | readFiles | local signing |
| `s3.PresignPut` | `bucket*`, `content_type`, `encryption`, `expires_seconds*`, `key*` | writeFiles | local signing |
| `native.AuthorizeAccount` | `{}` | selected account key | R |
| `native.ListBuckets` | `bucket_id`, `bucket_name`, `bucket_types` | listBuckets | R |
| `native.CreateBucket` | `bucket_info`, `bucket_name*`, `bucket_type*`, `cors_rules`, `default_server_side_encryption`, `file_lock_enabled`, `lifecycle_rules` | writeBuckets | W; access change |
| `native.UpdateBucket` | `bucket_id*`, `bucket_info`, `bucket_type`, `cors_rules`, `default_retention`, `default_server_side_encryption`, `file_lock_enabled`, `if_revision_is*`, `lifecycle_rules` | writeBuckets | W; access change |
| `native.DeleteBucket` | `bucket_id*` | deleteBuckets | W; destructive; access change |
| `native.ListKeys` | `max_key_count`, `start_application_key_id` | listKeys | R |
| `native.CreateKey` | `bucket_ids`, `capabilities*`, `key_name*`, `name_prefix`, `secret_output*`, `valid_duration_seconds` | writeKeys | W; access change |
| `native.DeleteKey` | `application_key_id*` | deleteKeys | W; destructive |
| `native.GetNotificationRules` | `bucket_id*` | readBucketNotifications | R |
| `native.SetNotificationRules` | `bucket_id*`, `rules*` | writeBucketNotifications | W; access change |

Encryption and lock fields may additionally require `writeBucketEncryption` or
`writeBucketRetentions`; governance bypass requires `bypassGovernance`.
Public access additionally requires `--allow-public`. Native capability checks
use the authorized key’s declared capabilities; the provider remains authoritative
for bucket/name-prefix scope and service permission enforcement.

Each direct operation has closed-request, dispatch, known-rejection, and SDK
serialization coverage in `test_backblaze_matrix.py`,
`test_backblaze_schema_matrix.py`, and `test_backblaze_sdk_serialization.py`.
Native HTTP method/query/token/deadline fixtures are in
`test_backblaze_native_transport.py`. Transfer ambiguity, local no-clobber,
backup, checkpoint and reconciliation coverage is in
`test_backblaze_transfer_recovery.py` and `test_backblaze_protocol.py`.
Secret delivery and endpoint boundaries have independent storage regression tests.
These are offline acceptance tests; live provider qualification is unperformed.

For `s3.GetObject`, `include_recovery_path: true` opts into the recovery-directory
field in the otherwise strict result-v1 payload; ordinary requests keep the
established result shape. A destination basename too long for the backup suffix
uses a deterministic bounded `.backblaze-backup-<16 hex>` sibling instead.

Multipart workflows compose CreateMultipartUpload, UploadPart, ListParts and
CompleteMultipartUpload. This helper accepts part sizes from 5 MiB–5 GiB and
uses 1 MiB bounded body reads; that range is a local workflow limit, not a general
provider limit claim.
PresignGet/PresignPut sign GetObject/PutObject requests and return sensitive URLs.
ListBuckets returns the complete documented list with no AWS pagination fields.
Object-list helpers expose bounded `max_pages`; other lists expose one page and
their provider continuation fields for an explicit subsequent request.

Native bucket names are create-only. Updates require `if_revision_is` and send
only explicitly selected mutable fields. Bucket information, CORS, lifecycle
and notification collections replace their corresponding complete collections.
File lock can only be enabled, never disabled. Default retention affects future
writes. The provider documents `defaultServerSideEncryption.mode: null` as a
reset to SSE-B2 and `defaultRetention.mode: null` as disabling default retention;
the local closed schema still rejects these null forms. When always-on SSE-B2 is
enabled for a bucket, new writes are encrypted and cannot be made unencrypted by
deleting its explicit encryption configuration.

Deleting an object name does not erase older versions. A supplied `version_id`
selects permanent version deletion. Object ACLs reflect their bucket ACL; B2
rejects independent object ACL changes. No bucket emptying, purge, or automatic
destructive recovery occurs.

Not exposed by this skill: POST presigning, S3 lifecycle APIs, S3 notification
APIs, bucket policies, S3 tag writes, website configuration, and
PutBucketVersioning. The provider supports S3 lifecycle Get/Put/Delete; this
skill manages lifecycle only through native bucket fields. If another client
manages lifecycle through S3, do not edit those rules through the web console or
native API because Backblaze regenerates S3 rule IDs. Backblaze IAM/STS bucket
policy operations are being released in phases; check operation-specific
availability. Also unsupported by B2 or this operation schema: SSE-KMS,
tagging/checksum directives ignored by B2, and undocumented request fields.
Event Notifications must be enabled for the account and allow up to 25 rules per
bucket. Account signup/billing and duplicate native object transfers are outside
this skill. See SKILL.md for setup, examples, error codes and recovery.
