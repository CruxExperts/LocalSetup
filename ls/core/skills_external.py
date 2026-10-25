"""Read-only discovery and source receipts for independently owned skills.

Discovery neither imports Python nor runs skill scripts. External originals are
never framework-managed packages; only separately materialized copies may be.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import islice
import os
from pathlib import Path
import re
import stat
from typing import Any

import yaml

CONTRACT_VERSION = 1
MAX_ENTRIES = 1024
MAX_FILES = 4096
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_SKILL_BYTES = 64 * 1024
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MANAGED_MARKERS = {".localsetup-managed.json", ".localsetup-managed"}


@dataclass(frozen=True)
class ExternalSkill:
    name: str
    scope: str
    path: Path
    description: str
    source_digest: str
    origin_id: str
    contract_version: int | None
    compatibility: str
    reason: str | None

    @property
    def selector(self) -> str:
        return f"{self.scope}:{self.name}"

    @property
    def package_id(self) -> str:
        return f"external:{self.origin_id}:{self.source_digest}:c{CONTRACT_VERSION}:{self.name}"

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": 1, "ownership": "external", "selector": self.selector,
            "package_id": self.package_id, "name": self.name, "scope": self.scope,
            "source_path": str(self.path), "source_digest": self.source_digest,
            "origin_id": self.origin_id, "contract_version": self.contract_version,
            "integration_contract": CONTRACT_VERSION,
        }

    def artifact_path(self, package_root: Path) -> Path:
        return binding_artifact_path(package_root, self.receipt())


def binding_artifact_path(package_root: Path, binding: dict[str, Any]) -> Path:
    """Validate a receipt before using any of its values as path components."""
    name = binding.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > 64:
        raise ValueError("External binding has an invalid visible name")
    for field in ("origin_id", "source_digest"):
        if not isinstance(binding.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", binding[field]):
            raise ValueError("External binding has an invalid identity digest")
    contract = binding.get("integration_contract")
    if type(contract) is not int or not 1 <= contract <= 1000:
        raise ValueError("External binding has an unsupported integration contract")
    expected_id = f"external:{binding['origin_id']}:{binding['source_digest']}:c{contract}:{name}"
    if binding.get("ownership") != "external" or binding.get("package_id") != expected_id:
        raise ValueError("External binding identity is inconsistent")
    path = package_root / ".external" / binding["origin_id"] / f"{binding['source_digest']}-c{contract}" / name
    if "artifact_path" in binding and Path(binding["artifact_path"]).absolute() != path.absolute():
        raise ValueError("External binding artifact is outside its selected package location")
    return path


def validate_source_binding(binding: dict[str, Any]) -> ExternalSkill:
    """Reject stale prepared work instead of updating a different source revision."""
    row = inspect_external_skill(Path(binding["source_path"]), binding["scope"])
    if any(binding.get(key) != value for key, value in row.receipt().items()):
        raise ValueError("External skill source changed; rebuild the selection and plan")
    if row.compatibility == "alignment-required":
        raise ValueError(f"External skill requires alignment: {row.reason}")
    return row


def _regular_bytes(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Skill input must be a bounded regular file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Skill file exceeds its byte budget")
        return data


def source_files(path: Path, *, managed_snapshot: bool = False) -> dict[str, tuple[bytes | None, int]]:
    """Snapshot a bounded plain tree; never follow links or include managed state."""
    if path.is_symlink() or not path.is_dir():
        raise ValueError("External skill must be a real directory")
    files: dict[str, tuple[bytes | None, int]] = {".": (None, path.stat().st_mode & 0o777)}
    if files["."][1] & 0o700 != 0o700:
        raise ValueError("Skill directories must be readable, writable and traversable by their owner")
    total = 0
    entries = 0
    pending = [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for entry in children:
                entries += 1
                if entries > MAX_FILES:
                    raise ValueError("Skill exceeds its entry budget")
                item = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError("External skill resources must not contain symlinks")
                if entry.is_dir(follow_symlinks=False):
                    mode = entry.stat(follow_symlinks=False).st_mode & 0o777
                    if mode & 0o700 != 0o700:
                        raise ValueError("Skill directories must be readable, writable and traversable by their owner")
                    files[item.relative_to(path).as_posix()] = (None, mode)
                    pending.append(item)
                    continue
                if entry.name in MANAGED_MARKERS:
                    if managed_snapshot and item.parent == path:
                        continue
                    raise ValueError("Managed package copies are not independent skill sources")
                data = _regular_bytes(item, min(MAX_FILE_BYTES, MAX_PACKAGE_BYTES - total))
                total += len(data)
                files[item.relative_to(path).as_posix()] = (data, item.stat().st_mode & 0o777)
    return files


def source_digest(files: dict[str, tuple[bytes | None, int]]) -> str:
    digest = hashlib.sha256(b"localsetup-external-skill-v1\0")
    for name, (data, mode) in sorted(files.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(b"d" if data is None else b"f")
        data = b"" if data is None else data
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def inspect_external_skill(path: Path, scope: str) -> ExternalSkill:
    if scope not in {"repo", "personal"}:
        raise ValueError("External skill scope must be repo or personal")
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("External skill source must not use symlink aliases")
    original = _regular_bytes(path / "SKILL.md", MAX_SKILL_BYTES)
    raw = original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        raise ValueError("Skill requires YAML frontmatter")
    frontmatter = yaml.safe_load(raw[4:raw.index("\n---\n", 4)])
    if not isinstance(frontmatter, dict):
        raise ValueError("Skill frontmatter must be a mapping")
    name, description = frontmatter.get("name"), frontmatter.get("description")
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name) or name != path.name:
        raise ValueError("Skill name must match its directory and use lowercase hyphenated words")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill requires a bounded nonempty description")
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in metadata.items()):
        raise ValueError("Skill metadata must map string keys to string values")
    contract = metadata.get("localsetup-contract")
    version = int(contract) if isinstance(contract, str) and re.fullmatch(r"[0-9]{1,4}", contract) else None
    compatible = version == CONTRACT_VERSION
    generic = contract is None
    files = source_files(path)
    if files.get("SKILL.md", (None, 0))[0] != original:
        raise ValueError("Skill source changed during inspection; inspect again")
    return ExternalSkill(
        name, scope, path, description, source_digest(files),
        hashlib.sha256(str(path).encode("utf-8")).hexdigest(),
        version if type(version) is int else None,
        "compatible" if compatible else "format-only" if generic else "alignment-required",
        None if compatible or generic else "Update metadata.localsetup-contract to the supported integration contract",
    )


def discover_external_skills(target: Path, home: Path) -> tuple[list[ExternalSkill], list[dict[str, str]]]:
    skills, findings = [], []
    seen: set[Path] = set()
    for scope, library in (("repo", target / ".agents/skills"), ("personal", home / ".agents/skills")):
        if not library.exists():
            continue
        if library.is_symlink() or not library.is_dir():
            findings.append({"scope": scope, "path": str(library), "reason": "Library must be a real directory"})
            continue
        with os.scandir(library) as entries:
            candidates = sorted(Path(entry.path) for entry in islice(entries, MAX_ENTRIES + 1))
        for index, path in enumerate(candidates):
            if index >= MAX_ENTRIES:
                findings.append({"scope": scope, "path": str(library), "reason": "Library exceeds its entry budget"})
                break
            # Managed adapter projections are not custom source libraries.
            if path.is_symlink() or not path.is_dir() or not (path / "SKILL.md").exists():
                continue
            if any((path / marker).exists() for marker in MANAGED_MARKERS):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                skills.append(inspect_external_skill(path, scope))
            except (OSError, ValueError, yaml.YAMLError, RecursionError) as exc:
                findings.append({"scope": scope, "path": str(path), "reason": str(exc)})
    return skills, findings
