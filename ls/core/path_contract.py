from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .lockfile import save_json
from .manifests import load_pack_config
from .paths import PathValidationError, expand_user_path, validate_repo_relative_path


PATHS_MANIFEST_VERSION = 1
PATHS_MANIFEST_NAME = "paths.json"
PATH_NAMES = {"source-root", "framework-root", "docs-root", "tools-root", "package-root"}


def localsetup_home(home: Path) -> Path:
    return home.expanduser().resolve(strict=False) / ".local" / "share" / "localsetup"


def paths_manifest_path(home: Path) -> Path:
    return localsetup_home(home) / PATHS_MANIFEST_NAME


def _safe_rel(value: str, field: str) -> str:
    return validate_repo_relative_path(value, field)


def _ensure_source_root(source_root: Path) -> Path:
    root = source_root.expanduser().resolve(strict=False)
    framework = root / "ls"
    if not framework.is_dir():
        raise PathValidationError(f"source-root must contain ls: {root}")
    return root


def build_paths_manifest(source_root: Path, home: Path) -> dict[str, Any]:
    source = _ensure_source_root(source_root)
    pack = load_pack_config(source)
    package_root = expand_user_path(pack.package_root, home).expanduser().resolve(strict=False)
    framework_root = source / "ls"
    payload: dict[str, Any] = {
        "schema_version": PATHS_MANIFEST_VERSION,
        "source_root": str(source),
        "framework_root": str(framework_root),
        "docs_root": str(framework_root / "docs"),
        "tools_root": str(framework_root / "tools"),
        "package_root": str(package_root),
        "paths": {
            "source-root": str(source),
            "framework-root": str(framework_root),
            "docs-root": str(framework_root / "docs"),
            "tools-root": str(framework_root / "tools"),
            "package-root": str(package_root),
        },
    }
    return payload


def write_paths_manifest(source_root: Path, home: Path) -> dict[str, Any]:
    payload = build_paths_manifest(source_root, home)
    path = paths_manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, payload)
    return {**payload, "manifest": str(path)}


def read_paths_manifest(home: Path) -> dict[str, Any] | None:
    path = paths_manifest_path(home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_invalid": f"invalid paths manifest: {path}"}
    return payload if isinstance(payload, dict) else {"_invalid": f"paths manifest must be an object: {path}"}


def paths_manifest_issues(source_root: Path, home: Path) -> list[str]:
    expected = build_paths_manifest(source_root, home)
    actual = read_paths_manifest(home)
    path = paths_manifest_path(home)
    if actual is None:
        return [f"missing paths manifest: {path}"]
    if actual.get("_invalid"):
        return [str(actual["_invalid"])]
    issues: list[str] = []
    for key in ("schema_version", "source_root", "framework_root", "docs_root", "tools_root", "package_root"):
        if actual.get(key) != expected.get(key):
            issues.append(f"stale paths manifest {key}: expected {expected.get(key)!r}, found {actual.get(key)!r}")
    actual_paths = actual.get("paths")
    if not isinstance(actual_paths, dict):
        issues.append("paths manifest paths must be an object")
    else:
        for name, value in expected["paths"].items():
            if actual_paths.get(name) != value:
                issues.append(f"stale paths manifest paths.{name}: expected {value!r}, found {actual_paths.get(name)!r}")
    return issues


def resolve_named_path(source_root: Path, home: Path, name: str) -> Path:
    if name not in PATH_NAMES:
        raise PathValidationError(f"unknown LocalSetup path name: {name}")
    payload = build_paths_manifest(source_root, home)
    return Path(payload["paths"][name])


def resolve_doc_path(source_root: Path, relative_path: str) -> Path:
    rel = _safe_rel(relative_path, "doc path")
    if rel.startswith("ls/docs/"):
        rel = rel.removeprefix("ls/docs/")
    return _ensure_source_root(source_root) / "ls" / "docs" / rel


def resolve_tool_path(source_root: Path, relative_path: str) -> Path:
    rel = _safe_rel(relative_path, "tool path")
    if rel.startswith("ls/tools/"):
        rel = rel.removeprefix("ls/tools/")
    return _ensure_source_root(source_root) / "ls" / "tools" / rel


def _canonical_package_name(source_root: Path, name: str) -> str:
    source = _ensure_source_root(source_root)
    if (source / "ls" / "skills" / name).is_dir() or (source / "ls" / "workflows" / name).is_dir():
        return name
    from .aliases import collect_package_aliases

    try:
        aliases = collect_package_aliases(source)
    except ValueError as exc:
        raise PathValidationError(f"ambiguous package alias: {exc}") from exc
    return aliases.get(name, name)


def resolve_package_path(home: Path, package_name: str, relative_path: str | None = None, *,
                         package_root: Path | None = None, source_root: Path | None = None) -> Path:
    name = _safe_rel(package_name, "package name")
    if "/" in name:
        raise PathValidationError(f"package name must not contain path separators: {package_name}")
    if source_root is not None:
        name = _canonical_package_name(source_root, name)
    root = package_root.expanduser().resolve(strict=False) if package_root else localsetup_home(home) / "packages"
    path = root / name
    if relative_path:
        rel = _safe_rel(relative_path, "package path")
        path = path / rel
    return path


def resolve_token(token: str, *, source_root: Path, home: Path, package_root: Path | None = None) -> Path:
    prefix = "localsetup://"
    if not token.startswith(prefix):
        raise PathValidationError(f"unsupported resolver token: {token}")
    body = token[len(prefix) :]
    kind, sep, remainder = body.partition("/")
    if not sep or not remainder:
        raise PathValidationError(f"resolver token must include a kind and path: {token}")
    if kind == "doc":
        return resolve_doc_path(source_root, remainder)
    if kind == "tool":
        return resolve_tool_path(source_root, remainder)
    if kind == "package":
        package, _sep, rel = remainder.partition("/")
        return resolve_package_path(home, package, rel or None, package_root=package_root,
                                    source_root=source_root)
    raise PathValidationError(f"unsupported resolver token kind: {kind}")
