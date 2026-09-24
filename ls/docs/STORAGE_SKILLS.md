---
status: ACTIVE
version: 5.6
owner_skill: ls-backblaze
---

# Optional storage skills

LocalSetup (LS) provides optional `ls-backblaze` and `ls-garage` skills for
storage operations. Select them through the integrations pack or individual
skill selection. Each installed skill contains its own Python package; neither
requires the LocalSetup Python distribution or the other provider's skill.

Use the [Backblaze skill](../skills/ls-backblaze/SKILL.md) or
[Garage skill](../skills/ls-garage/SKILL.md), its operation matrix, and offline request schema to
select an operation. Backblaze combines its S3-compatible API with B2 native
bucket, key, and notification administration. Garage combines its documented
S3 surface with a restricted Admin API v2 allowlist. Cluster deployment,
layout changes, repairs, purges, migrations, and admin-token management are
outside the Garage skill.

## Set up a caller-owned runtime

Python 3.12 or newer is required. Help, schemas, local plans, and native/admin
operations use the standard library. S3 execution uses the pinned SDK; a
missing SDK produces a structured `dependency_missing` error. The tools never
install dependencies automatically.

From the installed skill directory, create a dedicated environment at a path
you own and install its hashed export:

```bash
uv venv .venv-storage --python python3.12
uv pip install --python .venv-storage/bin/python --require-hashes \
  -r requirements-s3-sdk.txt
```

Both skills ship identical SDK requirements generated from the optional
`s3-sdk` group in the framework's authoritative lock. An existing environment
may be used when its dependency versions satisfy that export. Do not replace
an unrelated application's environment to run a skill.

## Plan, inspect, and apply

The entrypoints are `scripts/backblaze.py` and `scripts/garage.py`. Substitute
the provider entrypoint in these examples:

```bash
.venv-storage/bin/python scripts/backblaze.py --capabilities
.venv-storage/bin/python scripts/backblaze.py --schema request
.venv-storage/bin/python scripts/backblaze.py --schema result
.venv-storage/bin/python scripts/backblaze.py --tool s3.ListBuckets --args-json '{}'
```

Without `--apply`, execution produces a local plan with no network activity,
including no remote preflight. JSON arguments may also be read from
`--args-file request.json` or from stdin using `--args-file -`. Unknown fields,
unsupported operations, and invalid types are rejected locally. Object keys
and version identifiers are opaque values; do not normalize them as local
filesystem paths.

Apply mode requires an explicit provider configuration. Credentials use
selected environment-variable or protected-file references. The S3 clients
do not search the default AWS credential chain, instance metadata, or shell
credential processes. Garage requires separate S3 and admin endpoints and
credentials. HTTPS and certificate verification are required except for the
explicit Garage literal-loopback development option.

Destructive, access-changing, and public operations require the corresponding
CLI flags within the owner's authorized scope. The skills do not empty
buckets automatically. Treat presigned URLs and forms as sensitive intended
outputs; do not put them in ordinary logs, issue comments, or ledgers.

## Provider differences and recovery

| Concern | Backblaze | Garage |
|---|---|---|
| Version history | Name deletion and permanent version deletion are distinct; version identifiers must be explicit for historical deletion. | Version-history operations are rejected; do not assume prior-version recovery. |
| Encryption | Supported SSE-B2 and SSE-C operations are provider-specific. | SSE-C support does not imply bucket-default encryption support. |
| Access | Limited S3 ACL compatibility and native application-key capabilities. | Admin key/bucket owner, read, and write permissions; S3 ACLs and policies are rejected. |
| Replacement | Inspect the specific operation's recovery and version evidence. | Put, copy, and multipart completion require `--allow-overwrite`; conditional no-clobber guarantees are unestablished and requests requiring them are rejected. |
| Administrative compatibility | B2 request schemas and capability checks follow the maintained matrix. | Schema-tested against the reviewed v2.3.0 Admin API document, not every deployed Garage version. |

Safe reads use bounded retries. A mutation with an ambiguous response is
reported as `unknown` and must be reconciled through a read operation before
any new action. A read-before-write check does not prevent another writer
from replacing an object concurrently.

Multipart checkpoints bind the source identity, destination, upload, and
completed parts. Resume reconciles remote state; an uncertain completion
must not trigger automatic restart or abort. Downloads use a same-directory
temporary file and default to no-clobber. Explicit local replacement preserves
the prior regular file in an exclusive backup before publishing the new file.
Integrity reports describe the evidence actually available; multipart ETags
are not whole-file hashes.

New key secrets require an exclusively created protected output file. If
remote creation succeeds but delivery fails, reconcile the returned key
identifier; do not repeat creation or automatically revoke the key. Garage
key import is restricted to explicit restoration of an exported identity.

## Results and troubleshooting

Execution emits one versioned JSON envelope. Outcomes are `planned`,
`succeeded`, `partial`, `failed`, or `unknown`. Errors include a stable code,
retryability, and a reconciliation step when applicable.

| Exit | Meaning |
|---|---|
| 0 | Successful operation or local plan |
| 2 | Invalid input/configuration, unsupported capability, or missing dependency |
| 3 | Policy rejection |
| 4 | Known service or transport failure |
| 5 | Partial or unknown outcome |
| 130 | Interrupted execution |

For a missing dependency, install the skill's hashed export in the selected
environment. For an invalid field, inspect that provider's request schema;
AWS SDK acceptance alone does not establish provider support. For a policy
rejection, review the operation and existing owner authorization before
supplying its flag. For `partial` or `unknown`, use the returned reconciliation
guidance and preserve checkpoints and prior-file backups.

## Verification boundary

Acceptance installs the hashed SDK export into an isolated environment, then
uses network-denying SDK protocol stubs, native/admin fixtures, transfer
failure cases, and materialized isolated-runtime tests. Live Backblaze account
and Garage server qualification remains unperformed. Opt-in live suites must
be invoked separately with explicit configuration and execution authority.

Maintainers run focused and full tests with `uv run --locked --group s3-sdk`
and the repository's aggregate worker budget. Check export drift with
`python ls/tools/generate_s3_sdk_requirements.py --check` from the source
checkout. Run both owning documentation generators after registration changes.

Primary provider references:

- [Backblaze S3-compatible API](https://www.backblaze.com/docs/cloud-storage-s3-compatible-api)
- [Garage S3 compatibility](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/)
- [Garage Admin API schema](https://garagehq.deuxfleurs.fr/api/garage-admin-v2.json)
