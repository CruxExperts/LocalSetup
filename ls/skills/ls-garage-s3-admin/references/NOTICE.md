# Garage schema attribution and source

The Garage Admin API v2 schema is an upstream-derived asset. Copyright and
licensing remain with the Garage contributors. Its declared license is
**AGPL-3.0**, whose complete text is retained in
[LICENSE-AGPL-3.0.txt](LICENSE-AGPL-3.0.txt). The schema is not relicensed under
LocalSetup's MIT license.

The reviewed source was obtained from the
[official published schema](https://garagehq.deuxfleurs.fr/api/garage-admin-v2.json)
on 2026-09-08. It declares `info.version: v2.3.0` and has SHA-256
`07ffb2d0febbf386747eed0232b1a97931d323182f11e77537519620e6eb10d8`
(147,153 bytes). Its exact source bytes are preserved in
[upstream-admin-api-v2.json.gz](upstream-admin-api-v2.json.gz); decompress with
`gzip -dc upstream-admin-api-v2.json.gz` to read or modify the original JSON.

The installed `admin-api-v2.schema.json` is a LocalSetup modification made on
2026-09-08: it selects sixteen operations and their transitive component
schemas, removes documentation annotations from those schemas, and adds source
provenance metadata. It preserves the upstream required fields, constraints,
nullable forms, and alternatives. The LocalSetup source distribution includes
`ls/core/s3_sdk/garage_schema.py` and
`ls/tools/generate_garage_admin_schema.py` to reproduce that projection and the
source archive. The JSON itself is the editable source of the installed schema.
The archived full source is attribution material; it does not enable additional
runtime operations. The runtime separately enforces its explicit allowlist.

The selected sixteen paths and all forty-three referenced component schemas
are structurally identical to the corresponding definitions in the
[upstream v2.3.0 commit](https://github.com/deuxfleurs-org/garage/blob/7b119c0b4fa58ab3cb6d5db435fe52d990f6a7aa/doc/api/garage-admin-v2.json).
The full tagged schema has SHA-256
`32ad0fd9757def6d3d1d33b7ac79c4a5b62592100639729b9c69992a22dc2cfb`;
the published copy additionally describes layout-computation statistics outside
this skill's allowlist. These are distinct source artifacts, not identical bytes.

The upstream [license source](https://github.com/deuxfleurs-org/garage/blob/7b119c0b4fa58ab3cb6d5db435fe52d990f6a7aa/LICENSE)
is retained without modification. Its SHA-256 is
`0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`.
No live Garage server qualification is implied by schema or source comparison.
