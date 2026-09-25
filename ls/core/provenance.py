from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import provenance_source as _provenance_source
from .git_subprocess import run_git
from .lockfile import load_json
from .provenance_source import (
    GENERATED_DOCS_SUBJECT_PREFIX,
    GENERATED_SOURCE_DIRTY_PATHS,
    GENERATED_SOURCE_DIRTY_PREFIXES,
    VERSION_SYNC_SUBJECT_PREFIX,
    changed_paths_for_ref,
    framework_version,
    generated_artifact_parent_source_commit,
    generated_docs_source_ref,
    git_text,
    head_subject,
    is_generated_output_path,
    release_sync_parent_dirty,
    source_dirty,
    source_remote_url,
    source_root_id,
    source_root_id_for_commit,
    source_tag_for_ref,
    source_tree_sha,
    status_entry_paths,
    subject_for_ref,
    tree_sha_for_ref,
)
from .source import source_commit, source_tag


PROVENANCE_SCHEMA_VERSION = 1
MARKER_JSON = ".localsetup-managed.json"
MARKER_LEGACY = ".localsetup-managed"
PORTABLE_MARKER = ".localsetup-portable"

_changed_paths_for_ref = changed_paths_for_ref
_is_generated_output_path = is_generated_output_path
_status_entry_paths = status_entry_paths


def _sync_source_runner() -> None:
    _provenance_source.run_git = run_git


def _tree_sha_for_ref(repo_root: Path, ref: str) -> str:
    _sync_source_runner()
    return tree_sha_for_ref(repo_root, ref)


def source_tree_sha(repo_root: Path) -> str:
    _sync_source_runner()
    return _provenance_source.source_tree_sha(repo_root)


def _git_text(repo_root: Path, args: list[str]) -> str | None:
    _sync_source_runner()
    return git_text(repo_root, args)


def _source_tag_for_ref(repo_root: Path, ref: str) -> str | None:
    _sync_source_runner()
    return source_tag_for_ref(repo_root, ref)


def source_dirty(repo_root: Path) -> bool:
    _sync_source_runner()
    return _provenance_source.source_dirty(repo_root)


def _head_subject(repo_root: Path) -> str:
    _sync_source_runner()
    return head_subject(repo_root)


def _subject_for_ref(repo_root: Path, ref: str) -> str | None:
    _sync_source_runner()
    return subject_for_ref(repo_root, ref)


def _changed_paths_for_ref(repo_root: Path, ref: str) -> list[str]:
    _sync_source_runner()
    return changed_paths_for_ref(repo_root, ref)


def _generated_docs_source_ref(repo_root: Path, ref: str) -> str | None:
    _sync_source_runner()
    return generated_docs_source_ref(repo_root, ref)


def release_sync_parent_dirty(repo_root: Path) -> bool:
    _sync_source_runner()
    return _provenance_source.release_sync_parent_dirty(repo_root)


def generated_artifact_parent_source_commit(repo_root: Path) -> str | None:
    _sync_source_runner()
    return _provenance_source.generated_artifact_parent_source_commit(repo_root)


def source_remote_url(repo_root: Path) -> str | None:
    _sync_source_runner()
    return _provenance_source.source_remote_url(repo_root)


def framework_version(repo_root: Path) -> str:
    return _provenance_source.framework_version(repo_root)


def source_root_id(repo_root: Path) -> str:
    _sync_source_runner()
    return _provenance_source.source_root_id(repo_root)


def _source_root_id_for_commit(repo_root: Path, commit: str) -> str:
    _sync_source_runner()
    return source_root_id_for_commit(repo_root, commit)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_provenance_hash(payload: dict[str, Any]) -> str:
    keys = {
        "schema_version": payload.get("schema_version"),
        "framework_version": payload.get("framework_version"),
        "source_commit": payload.get("source_commit"),
        "source_tag": payload.get("source_tag"),
        "source_tree_sha": payload.get("source_tree_sha"),
        "source_dirty": payload.get("source_dirty"),
        "source_root_id": payload.get("source_root_id"),
    }
    return sha256_bytes(json.dumps(keys, sort_keys=True).encode("utf-8"))


def base_provenance(
    repo_root: Path,
    *,
    emitter: str,
    artifact_path: Path | None = None,
    package_name: str | None = None,
    package_type: str | None = None,
    generated_at: bool = False,
    generated_commit_parent: bool = False,
) -> dict[str, Any]:
    commit = source_commit(repo_root)
    tag = source_tag(repo_root)
    tree_sha = source_tree_sha(repo_root)
    dirty = source_dirty(repo_root)
    root_id = source_root_id(repo_root)
    if generated_commit_parent:
        parent = generated_artifact_parent_source_commit(repo_root)
        if parent:
            commit = parent
            tag = _source_tag_for_ref(repo_root, parent)
            tree_sha = _tree_sha_for_ref(repo_root, parent)
            dirty = release_sync_parent_dirty(repo_root)
            root_id = _source_root_id_for_commit(repo_root, parent)
    payload: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "framework_version": framework_version(repo_root),
        "source_commit": commit,
        "source_tag": tag,
        "source_tree_sha": tree_sha,
        "source_dirty": dirty,
        "source_root_id": root_id,
        "emitter": emitter,
    }
    if package_name:
        payload["package_name"] = package_name
    if package_type:
        payload["package_type"] = package_type
    if artifact_path is not None:
        payload["artifact_path"] = str(artifact_path)
    if generated_at:
        payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload["source_provenance_hash"] = source_provenance_hash(payload)
    return payload


def managed_marker_path(path: Path) -> Path:
    return path / MARKER_JSON


def legacy_marker_path(path: Path) -> Path:
    return path / MARKER_LEGACY


def is_managed_package(path: Path) -> bool:
    return managed_marker_path(path).exists() or legacy_marker_path(path).exists()


def has_legacy_marker(path: Path) -> bool:
    return legacy_marker_path(path).exists() and not managed_marker_path(path).exists()


def package_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    saw_file = False
    excluded = {MARKER_JSON, MARKER_LEGACY}
    for child in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        relative_path = child.relative_to(path)
        if (
            (len(relative_path.parts) == 1 and relative_path.name in excluded)
            or "__pycache__" in relative_path.parts
            or relative_path.suffix in {".pyc", ".pyo"}
        ):
            continue
        rel = relative_path.as_posix()
        saw_file = True
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
    return digest.hexdigest() if saw_file else None


def load_package_marker(path: Path) -> dict[str, Any] | None:
    marker = managed_marker_path(path)
    if marker.exists():
        return load_json(marker)
    legacy = legacy_marker_path(path)
    if legacy.exists():
        return {"schema_version": 0, "legacy_marker": True, "source": legacy.read_text(encoding="utf-8").strip()}
    return None


def build_package_marker(
    repo_root: Path,
    package_path: Path,
    *,
    package_name: str,
    package_type: str,
    source_path: Path,
    emitter: str,
    artifact_path: Path | None = None,
    installed_at: bool = True,
) -> dict[str, Any]:
    digest = package_digest(package_path)
    payload = base_provenance(
        repo_root,
        emitter=emitter,
        artifact_path=artifact_path or package_path,
        package_name=package_name,
        package_type=package_type,
        generated_at=installed_at,
    )
    payload.update(
        {
            "source_path": str(source_path),
            "package_digest": digest,
            "artifact_sha256": digest,
            "marker_path": str(managed_marker_path(artifact_path or package_path)),
        }
    )
    if installed_at and "generated_at" in payload:
        payload["installed_at"] = payload["generated_at"]
    return payload


def marker_public_snapshot(marker: dict[str, Any] | None) -> dict[str, Any] | None:
    if not marker:
        return None
    keys = [
        "schema_version",
        "framework_version",
        "source_commit",
        "source_tag",
        "source_tree_sha",
        "source_dirty",
        "source_root_id",
        "source_provenance_hash",
        "emitter",
        "package_name",
        "package_type",
        "source_path",
        "package_digest",
        "artifact_sha256",
        "installed_at",
        "legacy_marker",
        "source",
    ]
    return {key: marker.get(key) for key in keys if key in marker}


def markdown_with_provenance(text: str, provenance: dict[str, Any]) -> str:
    content_hash = sha256_bytes(text.encode("utf-8"))
    body = text
    existing: list[str] = []
    if body.startswith("---\n"):
        parts = body.split("---\n", 2)
        if len(parts) == 3:
            skip_localsetup_block = False
            for line in parts[1].splitlines():
                stripped = line.strip()
                if stripped.startswith("localsetup_provenance:"):
                    skip_localsetup_block = True
                    continue
                if skip_localsetup_block and (line.startswith(" ") or line.startswith("\t") or not stripped):
                    continue
                skip_localsetup_block = False
                if stripped.startswith(("framework_version:", "source_commit:", "artifact_sha256:")):
                    continue
                existing.append(line)
            body = parts[2].lstrip("\n")
    frontmatter = [
        "---",
        *existing,
        "localsetup_provenance:",
        f"  schema_version: {provenance['schema_version']}",
        f"  source_provenance_hash: {provenance['source_provenance_hash']}",
        f"  emitter: {provenance['emitter']}",
        f"framework_version: {provenance['framework_version']}",
        f"source_commit: {provenance['source_commit']}",
        f"artifact_sha256: {content_hash}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + body


def json_with_provenance(payload: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload)
    content = json.dumps(output, sort_keys=True, separators=(",", ":")).encode("utf-8")
    output["provenance"] = {**provenance, "artifact_sha256": sha256_bytes(content)}
    return output


def artifact_registry_entry(
    repo_root: Path,
    path: Path,
    *,
    artifact_type: str,
    emitter: str,
    source_inputs: list[str] | None = None,
    content_bytes: bytes | None = None,
    generated_commit_parent: bool = False,
) -> dict[str, Any]:
    provenance = base_provenance(
        repo_root,
        emitter=emitter,
        artifact_path=path,
        generated_commit_parent=generated_commit_parent,
    )
    data = content_bytes if content_bytes is not None else path.read_bytes()
    artifact_hash = sha256_bytes(data)
    return {
        "path": str(path.relative_to(repo_root) if path.is_absolute() and path.is_relative_to(repo_root) else path),
        "type": artifact_type,
        "emitter": emitter,
        "source_inputs": source_inputs or [],
        "artifact_sha256": artifact_hash,
        "provenance_hash": provenance["source_provenance_hash"],
        "source_commit": provenance["source_commit"],
        "framework_version": provenance["framework_version"],
    }


def _record_adapter_integrity_warnings(
    warnings: list[str], hints: list[str], adapter: dict[str, Any], repo_path: Path
) -> None:
    for failure in adapter.get("package_integrity_failures", []):
        package = failure.get("package")
        if package:
            warnings.append(f"scoped adapter package target differs from managed package: {package}")
            hints.append(f"refresh scoped adapter package link for {package} in {repo_path}")
        else:
            reason = failure.get("reason") or "unknown integrity failure"
            warnings.append(f"scoped adapter integrity failure: {repo_path}: {reason}")
            hints.append(f"refresh scoped adapter marker and package links in {repo_path}")


def provenance_report(
    repo_root: Path,
    *,
    lock: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    global_root: Path | None = None,
    adapters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    hints: list[str] = []
    package_status: dict[str, Any] = {}
    lock = lock or {}
    registry = registry or {}

    for package_name, expected in (lock.get("package_provenance") or {}).items():
        expected_digest = expected.get("package_digest") or expected.get("artifact_sha256")
        current_path = global_root / package_name if global_root else None
        current_digest = package_digest(current_path) if current_path else None
        if expected_digest and current_digest and expected_digest != current_digest:
            warnings.append(f"target lock references stale package digest for {package_name}")
            hints.append(f"refresh target package selection or reinstall {package_name}")
        if expected_digest and current_digest is None:
            warnings.append(f"target lock references missing global package digest for {package_name}")
            hints.append(f"reinstall missing managed package {package_name}")
        registry_digest = ((registry.get("packages") or {}).get(package_name) or {}).get("digest")
        if expected_digest and registry_digest and expected_digest != registry_digest:
            warnings.append(f"global registry digest differs from target lock for {package_name}")
            hints.append(f"run localsetup provenance report before planning repair for {package_name}")
        package_status[package_name] = {
            "lock_digest": expected_digest,
            "global_digest": current_digest,
            "registry_digest": registry_digest,
        }

    if global_root and global_root.exists():
        for package_path in sorted(p for p in global_root.iterdir() if p.is_dir()):
            if legacy_marker_path(package_path).exists():
                warnings.append(f"legacy plain managed marker found: {legacy_marker_path(package_path)}")
                hints.append(f"reinstall {package_path.name} to write {MARKER_JSON}")
            elif not managed_marker_path(package_path).exists() and package_path.name.startswith("ls-"):
                warnings.append(f"managed package marker missing JSON marker: {package_path}")
                hints.append(f"review unmanaged local content before replacing {package_path.name}")

    for adapter in adapters or []:
        repo_path = Path(str(adapter.get("repo_path")))
        _record_adapter_integrity_warnings(warnings, hints, adapter, repo_path)
        if adapter.get("points_to_global"):
            adapter["provenance_current"] = "global-managed-package"
        elif adapter.get("is_portable_copy"):
            adapter["provenance_current"] = "repo-portable-copy"
            if global_root and global_root.exists():
                managed_names = {str(name) for name in adapter.get("managed_visible_packages", [])}
                if not managed_names and "managed_visible_packages" not in adapter:
                    managed_names = {path.name for path in repo_path.iterdir() if path.is_dir()}
                for package_path in sorted(p for p in repo_path.iterdir() if p.is_dir() and p.name in managed_names):
                    local_digest = package_digest(package_path)
                    global_digest = package_digest(global_root / package_path.name)
                    if local_digest and global_digest and local_digest != global_digest:
                        warnings.append(f"portable adapter package differs from global package: {package_path.name}")
                        hints.append(f"portable adapter remains current for this repo; compare before refreshing {package_path.name}")
        elif adapter.get("is_scoped_symlink_adapter"):
            adapter["provenance_current"] = "repo-scoped-symlink-adapter"
            visible = set(str(name) for name in adapter.get("managed_visible_packages", adapter.get("visible_packages", [])))
            expected = set(str(name) for name in adapter.get("expected_packages", []))
            if expected and visible != expected:
                warnings.append(f"scoped adapter package set differs from target lock: {repo_path}")
                hints.append(f"refresh adapter package selection for {repo_path}")
        elif adapter.get("exists"):
            adapter["provenance_current"] = "unmanaged-local-content"

    registry_path = repo_root / "ls" / "docs" / "_generated" / "artifact-registry.json"
    if registry_path.exists():
        artifact_registry = load_json(registry_path)
        for entry in artifact_registry.get("artifacts", []):
            rel = entry.get("path")
            if not rel:
                continue
            artifact_path = repo_root / str(rel)
            if not artifact_path.exists():
                warnings.append(f"generated artifact missing from registry path: {rel}")
                hints.append(f"regenerate docs artifact {rel}")
                continue
            current_hash = sha256_file(artifact_path)
            if entry.get("artifact_sha256") and entry.get("artifact_sha256") != current_hash:
                warnings.append(f"generated artifact has stale content digest: {rel}")
                hints.append(f"regenerate docs artifact {rel}")

    return {
        "ok": True,
        "warnings": sorted(set(warnings)),
        "repair_hints": sorted(set(hints)),
        "packages": package_status,
    }
