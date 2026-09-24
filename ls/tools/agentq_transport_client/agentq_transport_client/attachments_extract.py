#!/usr/bin/env python3
# Purpose: Extract authenticated manifest attachments and verify SHA-256.
# Created: 2026-03-09
# Last updated: 2026-03-09

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from agentq_transport_client.crypto_pipeline import CryptoPipelineError

_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _safe_relpath(path: str) -> Path:
    """Reject absolute paths and path traversal."""
    p = Path(path)
    if p.is_absolute():
        raise CryptoPipelineError("MANIFEST_INVALID", "attachment path must be relative.")
    parts = p.parts
    if ".." in parts or parts[0] == "/":
        raise CryptoPipelineError("MANIFEST_INVALID", "attachment path traversal rejected.")
    return Path(*parts)


def extract_attachments_to_staging(
    staging_dir: Path,
    manifest: dict[str, Any],
) -> None:
    """
    For each attachment with content_b64, decode, sha256 verify, write under staging_dir/attachments/.
    Strips content_b64 from manifest copy on disk only if we write manifest.json after (caller writes prd only then skip).
    Raises CryptoPipelineError ingest_checksum_fail on mismatch.
    """
    att = manifest.get("attachments")
    if not att or not isinstance(att, list):
        return
    base = staging_dir / "attachments"
    base.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(att):
        if not isinstance(row, dict):
            continue
        sha = (row.get("sha256") or "").lower()
        if not _SHA256.match(sha):
            continue
        b64 = row.get("content_b64")
        if not b64 or not isinstance(b64, str):
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:
            raise CryptoPipelineError(
                "MANIFEST_INVALID", f"attachments[{i}] content_b64 invalid: {exc}"
            ) from exc
        digest = hashlib.sha256(raw).hexdigest()
        if digest != sha:
            raise CryptoPipelineError(
                "ingest_checksum_fail",
                f"attachments[{i}] sha256 mismatch expected {sha} got {digest}",
            )
        rel = _safe_relpath(row.get("path") or f"file_{i}")
        out = base / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
