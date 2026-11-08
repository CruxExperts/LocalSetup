---
name: ls-wf-organize-user-files
description: Organize personal Documents, Pictures, Videos, Downloads, invoices, and other user-space files by purpose; research uncertain provenance, keep one readable folder index, and report recoverable disposal. Excludes software repository maintenance and automatic cloud synchronization.
metadata:
  version: "1.0"
---

# Organize user files

Use this workflow for user-owned files, not software repository maintenance.
Do not initialize Git or use Git to manage personal files; preserve any existing
Git metadata and report it for review. Agree the target scope from the request,
inspect existing organization and destinations, and use
human-readable folder and file names that describe purpose or content. Keep one
`index.yaml` per organized folder, following the versioned
[index schema](references/user-folder-index.schema.json); it describes direct
children in concise prose; use each direct child name at most once and omit the
index itself. Do not require a README, AGENTS.md, extra metadata
files, or Git in user space. Keep private details in the user's private space.

Inventory and classify by purpose and file type. Inspect enough content to
distinguish incoming invoices from invoices billed to clients, and keep invoices
in the established invoices folder. Before making an extra cloud copy, consult
the existing authorized registry and destination for a matching item; resolve
identity and uncertainty before copying. Do not upload or disclose files without
authorization for the destination and scope. Investigate uncertain origin and
currency against actual sources; mark unresolved facts as uncertain, never as
verified provenance.

Preserve originals while identifying and processing user-generated image
watermarks with the existing removal skill and installed tools. Use separate,
descriptively named outputs, preserve non-AI branding and third-party notices,
and report what each tool could and could not detect or remove. Do not claim a
clean result for an unverified or unsupported watermark. Keep originals and
outputs associated in the index; do not remove third-party paid-asset marks.

Keep uncertain or potentially useful files. Move approved discards to the user's
Trash or a clearly named quarantine with recovery available; do not permanently
delete them. Record each discarded path, reason, disposition, and recovery
location in the run report. Do not treat identical names as proof of duplicate
content. Route scattered configuration backups to the existing approved backup
owner and state location, preserving recovery and retention requirements; do not
invent a second store, alter global policy, or remove originals until the
authorized archive operation and its required verification have succeeded.

Report the sorted destinations, index updates, disposition and recovery report,
unresolved provenance, unprocessed images, and any permissions or service limits.
This workflow may be run again on request; it does not install a scheduler,
configure a mirror, or authorize ongoing cloud writes.
