"""Repository coverage and exact exception/asset-review validation."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

from ..aliases import collect_skill_aliases, legacy_skill_name
from .rules import EXCEPTION_KINDS, references


VISUAL_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".ico", ".pdf", ".bmp", ".tif", ".tiff"}


def _visual_data(data: bytes) -> bool:
    return data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\0", b"MM\0*", b"%PDF-", b"\0\0\1\0")) or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")


def _safe_path(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not PurePosixPath(value).is_absolute() and ".." not in PurePosixPath(value).parts


def load_policy(path: Path) -> dict:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise ValueError("branding policy must have schema_version 1")
    for key in ("exceptions", "visual_reviews", "binary_reviews"):
        if not isinstance(policy.get(key), list):
            raise ValueError(f"branding policy {key} must be a list")
    identities = set()
    for item in policy["exceptions"]:
        if not isinstance(item, dict) or not _safe_path(item.get("path")):
            raise ValueError("branding exception requires a repository-relative path")
        if not isinstance(item.get("kind"), str) or item["kind"] not in EXCEPTION_KINDS or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("branding exception requires an allowed kind and reason")
        digest = item.get("line_sha256", "")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("branding exception requires a lowercase SHA-256")
        if not isinstance(item.get("token"), str) or not item["token"]:
            raise ValueError("branding exception requires a token")
        if type(item.get("count")) is not int or item["count"] < 1:
            raise ValueError("branding exception requires a positive occurrence count")
        identity = (item["path"], item["line_sha256"], item["token"])
        if identity in identities:
            raise ValueError("duplicate branding exception")
        identities.add(identity)
    seen = set()
    for item in policy["visual_reviews"]:
        if not isinstance(item, dict) or not _safe_path(item.get("path")) or item["path"] in seen:
            raise ValueError("visual review requires a unique repository-relative path")
        seen.add(item["path"])
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("sha256", "reviewed_text", "accessibility_evidence", "reviewer", "reviewed_at")):
            raise ValueError("visual review requires hash, visual and accessibility evidence, reviewer and date")
        if len(item["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in item["sha256"]):
            raise ValueError("visual review requires a lowercase SHA-256")
    seen = set()
    for item in policy["binary_reviews"]:
        if not isinstance(item, dict) or not _safe_path(item.get("path")) or item["path"] in seen:
            raise ValueError("binary review requires a unique repository-relative path")
        seen.add(item["path"])
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("binary review requires a nonvisual classification reason")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("binary review requires a lowercase SHA-256")
    return policy


def repository_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True,
    )
    paths = sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""})
    if not all(_safe_path(path) for path in paths):
        raise ValueError("Git inventory contains an unsafe repository-relative path")
    return paths


def _policy_token_lines(name: str, text: str, policy: dict) -> set[int]:
    # Only the owning, validated, canonically serialized policy has metadata
    # tokens. Arbitrary JSON token fields and every human rationale stay scanned.
    if name != "ls/config/branding.json" or text != json.dumps(policy, indent=2) + "\n":
        return set()
    inside, result = False, set()
    for number, line in enumerate(text.splitlines(), 1):
        if line == '  "exceptions": [':
            inside = True
        elif inside and line == '  ],':
            inside = False
        elif inside and line.startswith('      "token": '):
            result.add(number)
    return result


def scan(root: Path, policy: dict, *, paths: list[str] | None = None) -> dict:
    inventory, rows, findings = [], [], []
    names = repository_paths(root) if paths is None else sorted(set(paths))
    if not all(_safe_path(name) for name in names):
        raise ValueError("unsafe scan path")
    source_names = set()
    for name in names:
        parts = PurePosixPath(name).parts
        if len(parts) == 4 and parts[:2] == ("ls", "skills") and parts[2].startswith("ls-") and parts[3] == "SKILL.md":
            source = root / name
            if not any(p.is_symlink() for p in (source, source.parent, source.parent.parent, source.parent.parent.parent)) and source.is_file():
                source_names.add(parts[2])
    identifiers = source_names | {legacy_skill_name(name) for name in source_names}
    pack_name = "ls/config/pack.yaml"
    pack_path = root / pack_name
    if source_names and pack_name in names and not any(
        path.is_symlink() for path in (pack_path, pack_path.parent, pack_path.parent.parent)
    ) and pack_path.is_file():
        aliases = collect_skill_aliases(root / "ls" / "skills")
        identifiers.update(alias for alias, target in aliases.items() if target in source_names)
    identifiers = frozenset(identifiers)
    for name in names:
        if not _safe_path(name):
            raise ValueError("unsafe scan path")
        path = root / name
        # Do not follow a symlink in any path component outside the inventory.
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root and root in parent.parents):
            inventory.append({"path": name, "surface": "symlink", "ownership": "compatibility_link"})
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append({"code": "unreadable_path", "path": name})
            continue
        digest = hashlib.sha256(data).hexdigest()
        visual = path.suffix.lower() in VISUAL_SUFFIXES or _visual_data(data)
        try:
            text = data.decode("utf-8") if b"\0" not in data else None
        except UnicodeDecodeError:
            text = None
        surface = "visual" if visual else "text" if text is not None else "binary"
        inventory.append({"path": name, "surface": surface, "sha256": digest,
                          "ownership": "generated" if "/_generated/" in name else "repository"})
        if text is not None:
            file_rows = references(name, text, identifiers=identifiers)
            metadata_lines = _policy_token_lines(name, text, policy)
            for row in file_rows:
                if row["line"] in metadata_lines:
                    row["classification"] = "policy_metadata"
            rows.extend(file_rows)
    unresolved = Counter((r["path"], r["line_sha256"], r["token"]) for r in rows if r["classification"] == "unclassified")
    accepted = {}
    for exception in policy["exceptions"]:
        key = (exception["path"], exception["line_sha256"], exception["token"])
        if unresolved[key] != exception["count"]:
            findings.append({"code": "stale_exception", "path": exception["path"], "token": exception["token"], "expected_count": exception["count"], "actual_count": unresolved[key]})
        else:
            accepted[key] = exception["kind"]
    for row in rows:
        key = (row["path"], row["line_sha256"], row["token"])
        if row["classification"] == "unclassified":
            if key in accepted:
                row["classification"] = accepted[key]
            else:
                findings.append({"code": "unclassified_reference", **row})
    visuals = {item["path"]: item for item in inventory if item["surface"] == "visual"}
    reviewed = set()
    for review in policy["visual_reviews"]:
        item = visuals.get(review["path"])
        if not item or item["sha256"] != review["sha256"]:
            findings.append({"code": "stale_visual_review", "path": review["path"]})
        else:
            reviewed.add(review["path"])
    findings.extend({"code": "visual_review_required", "path": name, "sha256": item["sha256"]} for name, item in visuals.items() if name not in reviewed)
    binaries = {item["path"]: item for item in inventory if item["surface"] == "binary"}
    classified = set()
    for review in policy["binary_reviews"]:
        item = binaries.get(review["path"])
        if not item or item["sha256"] != review["sha256"]:
            findings.append({"code": "stale_binary_review", "path": review["path"]})
        else:
            classified.add(review["path"])
    findings.extend({"code": "binary_classification_required", "path": name, "sha256": item["sha256"]} for name, item in binaries.items() if name not in classified)
    return {"schema_version": 1, "ok": not findings, "inventory": inventory, "references": rows, "findings": findings,
            "counts": {"files": len(inventory), "references": len(rows), "findings": len(findings)}}
