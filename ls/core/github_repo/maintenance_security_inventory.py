"""Read bounded, immutable GitHub lockfile blobs without running candidate code."""

from __future__ import annotations

import base64
import binascii
import json
import re
import tomllib
from typing import Any


LOCK_NAMES = {"uv.lock", "Cargo.lock", "bun.lock"}
UNSUPPORTED_LOCK_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "Pipfile.lock", "Gemfile.lock", "composer.lock", "go.sum",
}
MAX_LOCK_BYTES = 8 * 1024 * 1024
MAX_TREE_ENTRIES = 100_000
MAX_PACKAGES = 20_000
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9@._/+:-]{1,200}")
_VERSION = re.compile(r"[A-Za-z0-9.+_~-]{1,100}")


class InventoryUnknown(Exception):
    """An immutable source could not establish supported lockfile coverage."""


def _bun_json(text: str) -> Any:
    """Strip JSONC comments and trailing commas only outside quoted strings."""
    output: list[str] = []
    index = 0
    quoted = False
    escaped = False
    while index < len(text):
        char = text[index]
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            output.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise InventoryUnknown("bun_lock_invalid")
            index = end + 2
            continue
        output.append(char)
        index += 1
    if quoted:
        raise InventoryUnknown("bun_lock_invalid")
    stripped = "".join(output)
    output = []
    quoted = escaped = False
    for index, char in enumerate(stripped):
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        if char == "," and stripped[index + 1 :].lstrip().startswith(("]", "}")):
            continue
        output.append(char)
    try:
        return json.loads("".join(output))
    except json.JSONDecodeError:
        raise InventoryUnknown("bun_lock_invalid") from None


def parse_lockfile(name: str, data: bytes) -> tuple[tuple[str, str, str], ...]:
    """Return ecosystem/name/resolved-version tuples for native supported locks."""
    if len(data) > MAX_LOCK_BYTES:
        raise InventoryUnknown("lock_too_large")
    try:
        text = data.decode("utf-8")
        if name in {"uv.lock", "Cargo.lock"}:
            document = tomllib.loads(text)
        elif name == "bun.lock":
            document = _bun_json(text)
        else:
            raise InventoryUnknown("lock_unsupported")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise InventoryUnknown("lock_invalid") from None
    if not isinstance(document, dict):
        raise InventoryUnknown("lock_invalid")
    records: list[tuple[str, str, str]] = []
    if name == "bun.lock":
        packages = document.get("packages")
        if document.get("lockfileVersion") != 1 or not isinstance(packages, dict):
            raise InventoryUnknown("bun_lock_invalid")
        for package_name, row in packages.items():
            if not isinstance(package_name, str) or not _NAME.fullmatch(package_name):
                raise InventoryUnknown("bun_lock_invalid")
            if not isinstance(row, list) or not row or not isinstance(row[0], str):
                raise InventoryUnknown("bun_lock_invalid")
            resolved = row[0]
            resolved_name, separator, version = resolved.rpartition("@")
            if not separator or not _NAME.fullmatch(resolved_name):
                raise InventoryUnknown("bun_lock_unsupported_source")
            if version.startswith("workspace:"):
                continue
            if not _VERSION.fullmatch(version):
                raise InventoryUnknown("bun_lock_unsupported_source")
            records.append(("npm", resolved_name, version))
    else:
        packages = document.get("package")
        if not isinstance(packages, list):
            raise InventoryUnknown("lock_invalid")
        for row in packages:
            if not isinstance(row, dict):
                raise InventoryUnknown("lock_invalid")
            source = row.get("source")
            if name == "uv.lock":
                if not isinstance(source, dict):
                    raise InventoryUnknown("lock_source_unverified")
                if source.get("editable") is not None or source.get("virtual") is not None:
                    continue
                if source.get("registry") != "https://pypi.org/simple":
                    raise InventoryUnknown("lock_source_unverified")
                ecosystem = "pip"
            else:
                if source is None:
                    continue  # local workspace crate, not a published dependency
                if source != "registry+https://github.com/rust-lang/crates.io-index":
                    raise InventoryUnknown("lock_source_unverified")
                ecosystem = "rust"
            package_name, version = row.get("name"), row.get("version")
            if not isinstance(package_name, str) or not _NAME.fullmatch(package_name) or not isinstance(version, str) or not _VERSION.fullmatch(version):
                raise InventoryUnknown("lock_invalid")
            records.append((ecosystem, package_name, version))
    if len(records) > MAX_PACKAGES:
        raise InventoryUnknown("lock_too_large")
    return tuple(sorted(set(records)))


def parse_git_blob(blob: Any, expected_sha: str) -> bytes:
    if not isinstance(blob, dict) or blob.get("sha") != expected_sha or blob.get("encoding") != "base64":
        raise InventoryUnknown("git_blob_invalid")
    content = blob.get("content")
    if not isinstance(content, str) or len(content) > MAX_LOCK_BYTES * 2:
        raise InventoryUnknown("git_blob_invalid")
    try:
        data = base64.b64decode(content, validate=False)
    except (ValueError, binascii.Error):
        raise InventoryUnknown("git_blob_invalid") from None
    if len(data) > MAX_LOCK_BYTES or blob.get("size") != len(data):
        raise InventoryUnknown("git_blob_invalid")
    return data


def lock_blobs_from_tree(tree: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(tree, dict) or tree.get("truncated") is not False:
        raise InventoryUnknown("git_tree_incomplete")
    entries = tree.get("tree")
    if not isinstance(entries, list) or len(entries) > MAX_TREE_ENTRIES:
        raise InventoryUnknown("git_tree_incomplete")
    locks: list[tuple[str, str]] = []
    for row in entries:
        if not isinstance(row, dict):
            raise InventoryUnknown("git_tree_incomplete")
        path = row.get("path")
        if not isinstance(path, str):
            raise InventoryUnknown("git_tree_incomplete")
        basename = path.rsplit("/", 1)[-1]
        if basename in UNSUPPORTED_LOCK_NAMES:
            raise InventoryUnknown("lock_unsupported")
        if basename in LOCK_NAMES:
            sha = row.get("sha")
            if row.get("type") != "blob" or not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise InventoryUnknown("git_tree_incomplete")
            locks.append((basename, sha))
    return tuple(locks)
