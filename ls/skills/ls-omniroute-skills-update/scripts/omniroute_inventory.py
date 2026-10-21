#!/usr/bin/env python3
"""Extract deterministic OmniRoute source inventories and verify retained claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SOURCE_TAG = "v3.8.48"
SOURCE_TAG_OBJECT = "4f00f84b5a12f90fca2f1d72a60404cf6f5bf059"
SOURCE_COMMIT = "7ee5bbc64dbb03e967521227f2afffeb7c9dad1e"
SOURCE_TREE = "4048504f76c6fb3dedd00ff2aa7250109308de99"
SOURCE_SKILLS_TREE = "e7b1871e0904fbdb0ff01bdc3fc1d7ea599707ff"
OPENAPI_PATH = "docs/openapi.yaml"
REWRITE_PATH = "next.config.mjs"
HTTP_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")
SKILL_PATH_RE = re.compile(r"^skills/[^/]+/SKILL\.md$")
SKILL_NAME_RE = re.compile(r"(?m)^name:\s*([a-z0-9][a-z0-9-]*)\s*$")
DIRECT_TOOL_RE = re.compile(
    r"server\.registerTool\(\s*[\"'](omniroute_[a-z0-9_]+)[\"']"
)
NAMED_TOOL_RE = re.compile(r"\bname:\s*[\"'](omniroute_[a-z0-9_]+)[\"']")
METHOD_EXPORT_RE = re.compile(
    r"\bexport\s+(?:async\s+function|const)\s+"
    r"(DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)\b"
)
CLAIM_ENDPOINT_RE = re.compile(
    r"\b((?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)"
    r"(?:/(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT))*)\s+"
    r"(/[A-Za-z0-9._~!$&'()*+,;=:@%/{}<>\[\]*:-]+)"
)
CLAIM_ENDPOINT_FIRST_TABLE_RE = re.compile(
    r"(?m)^[ \t]*\|[ \t]*`?"
    r"(/[A-Za-z0-9._~!$&'()*+,;=:@%/{}<>\[\]*:-]+)"
    r"`?[ \t]*\|[ \t]*"
    r"(?:HTTP[ \t]+)?"
    r"((?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)"
    r"(?:/(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT))*)"
    r"[ \t]*\|"
)
CLAIM_METHOD_FIRST_TABLE_RE = re.compile(
    r"(?m)^[ \t]*\|[ \t]*"
    r"(?:HTTP[ \t]+)?"
    r"((?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)"
    r"(?:/(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT))*)"
    r"[ \t]*\|[ \t]*`?"
    r"(/[A-Za-z0-9._~!$&'()*+,;=:@%/{}<>\[\]*:-]+)"
    r"`?[ \t]*\|"
)
CLAIM_TOOL_RE = re.compile(r"\bomniroute_[a-z0-9_]+\b")
RETAINED_PACKAGES = (
    "ls-omniroute-task-triage",
    "ls-omniroute-admin-automation",
    "ls-omniroute-model-discovery",
    "ls-omniroute-skills-update",
)
TOOL_OWNER_PATHS = (
    "open-sse/mcp-server/server.ts",
    "open-sse/mcp-server/toolSearch/register.ts",
    "open-sse/mcp-server/tools/agentSkillTools.ts",
    "open-sse/mcp-server/tools/compressionTools.ts",
    "open-sse/mcp-server/tools/gamificationTools.ts",
    "open-sse/mcp-server/tools/githubSkillTools.ts",
    "open-sse/mcp-server/tools/memoryTools.ts",
    "open-sse/mcp-server/tools/notionTools.ts",
    "open-sse/mcp-server/tools/obsidianTools.ts",
    "open-sse/mcp-server/tools/pluginTools.ts",
    "open-sse/mcp-server/tools/poolTools.ts",
    "open-sse/mcp-server/tools/skillTools.ts",
)
CLAIM_SUFFIXES = {".json", ".md", ".py", ".yaml", ".yml"}
SUPPORTED_SUFFIX_WILDCARD_CLAIMS = {
    ("GET", "/api/combos*"),
    ("POST", "/api/combos*"),
    ("GET", "/api/pricing*"),
    ("GET", "/api/provider-nodes*"),
    ("POST", "/api/provider-nodes*"),
    ("GET", "/api/providers*"),
    ("POST", "/api/providers*"),
    ("GET", "/api/usage/*"),
    ("POST", "/v1/audio/*"),
}
MISSING_PYYAML_MESSAGE = (
    "Missing dependency: PyYAML. Run `uv sync --locked --no-dev` from the "
    "LocalSetup source checkout."
)

# Every exception is explicit, immutable-source-backed, and checked for a real
# source object. W94 fills this table only for retained claims that are
# intentionally version-dependent rather than registered in v3.8.48.
CLAIM_EXCEPTIONS: dict[str, dict[str, str]] = {}


class InventoryError(RuntimeError):
    """Stable, sanitized immutable-inventory failure."""


def _load_yaml() -> Any:
    """Load PyYAML only for the OpenAPI operation that requires it."""
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            raise InventoryError(MISSING_PYYAML_MESSAGE) from None
        raise
    return yaml


def _git(
    git_dir: Path,
    *args: str,
    text: bool = True,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", f"--git-dir={git_dir}", *args],
            check=False,
            capture_output=True,
            text=text,
        )
    except OSError:
        raise InventoryError("inventory_git_unavailable") from None
    if result.returncode not in allowed_returncodes:
        raise InventoryError("inventory_git_read_failed")
    return result.stdout


def _blob(git_dir: Path, path: str) -> bytes:
    payload = _git(git_dir, "show", f"{SOURCE_COMMIT}:{path}", text=False)
    assert isinstance(payload, bytes)
    return payload


def _object_id(git_dir: Path, expression: str) -> str:
    value = str(_git(git_dir, "rev-parse", expression)).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise InventoryError("inventory_object_invalid")
    return value


def _source_provenance(git_dir: Path) -> dict[str, str]:
    """Verify the immutable annotated release tag before reading source content."""
    tag_ref = f"refs/tags/{SOURCE_TAG}"
    try:
        tag_object = _object_id(git_dir, tag_ref)
    except InventoryError:
        raise InventoryError("inventory_source_tag_missing") from None
    if tag_object != SOURCE_TAG_OBJECT:
        raise InventoryError("inventory_source_tag_mismatch")
    try:
        object_type = str(_git(git_dir, "cat-file", "-t", tag_object)).strip()
    except InventoryError:
        raise InventoryError("inventory_source_tag_not_annotated") from None
    if object_type != "tag":
        raise InventoryError("inventory_source_tag_not_annotated")
    try:
        commit = _object_id(git_dir, f"{tag_object}^{{commit}}")
        tree = _object_id(git_dir, f"{commit}^{{tree}}")
        skills_tree = _object_id(git_dir, f"{commit}:skills")
    except InventoryError:
        raise InventoryError("inventory_source_tag_peel_failed") from None
    if (
        commit != SOURCE_COMMIT
        or tree != SOURCE_TREE
        or skills_tree != SOURCE_SKILLS_TREE
    ):
        raise InventoryError("inventory_source_tag_peel_failed")
    return {
        "tag": SOURCE_TAG,
        "tag_object": tag_object,
        "commit": commit,
        "tree": tree,
        "skills_tree": skills_tree,
    }


def _sha256_json(value: Any) -> str:
    rendered = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _receipt(git_dir: Path, path: str) -> dict[str, str]:
    payload = _blob(git_dir, path)
    return {
        "source_path": path,
        "source_blob": _object_id(git_dir, f"{SOURCE_COMMIT}:{path}"),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _tracked_paths(git_dir: Path) -> list[str]:
    output = str(_git(git_dir, "ls-tree", "-r", "--name-only", SOURCE_COMMIT))
    paths = [line for line in output.splitlines() if line]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise InventoryError("inventory_tree_invalid")
    return paths


def _skill_inventory(git_dir: Path, paths: list[str]) -> list[dict[str, str]]:
    skills: list[dict[str, str]] = []
    for path in paths:
        if not SKILL_PATH_RE.fullmatch(path):
            continue
        payload = _blob(git_dir, path)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise InventoryError("inventory_skill_invalid") from None
        match = SKILL_NAME_RE.search(text)
        if not match:
            raise InventoryError("inventory_skill_invalid")
        skills.append(
            {
                "name": match.group(1),
                "path": path,
                "blob": _object_id(git_dir, f"{SOURCE_COMMIT}:{path}"),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    skills.sort(key=lambda row: (row["name"], row["path"]))
    if len({row["name"] for row in skills}) != len(skills):
        raise InventoryError("inventory_skill_duplicate")
    return skills


def _openapi_inventory(git_dir: Path) -> list[dict[str, str]]:
    receipt = _receipt(git_dir, OPENAPI_PATH)
    yaml = _load_yaml()
    try:
        document = yaml.safe_load(_blob(git_dir, OPENAPI_PATH))
    except yaml.YAMLError:
        raise InventoryError("inventory_openapi_invalid") from None
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(paths, dict):
        raise InventoryError("inventory_openapi_invalid")
    operations: list[dict[str, str]] = []
    for route, methods in paths.items():
        if not isinstance(route, str) or not route.startswith("/") or not isinstance(methods, dict):
            raise InventoryError("inventory_openapi_invalid")
        for method in HTTP_METHODS:
            operation = methods.get(method.lower())
            if not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId")
            operations.append(
                {
                    "method": method,
                    "path": route,
                    "operation_id": operation_id if isinstance(operation_id, str) else "unknown",
                    **receipt,
                }
            )
    operations.sort(key=lambda row: (row["path"], row["method"], row["operation_id"]))
    return operations


def _route_from_source_path(path: str) -> str:
    relative = path.removeprefix("src/app/").removesuffix("/route.ts")
    parts = [part for part in relative.split("/") if not (part.startswith("(") and part.endswith(")"))]
    route = "/" + "/".join(parts)
    route = re.sub(r"\[\[\.\.\.([^\]]+)\]\]", r"{\1*}", route)
    route = re.sub(r"\[\.\.\.([^\]]+)\]", r"{\1*}", route)
    route = re.sub(r"\[([^\]]+)\]", r"{\1}", route)
    return route


def _route_inventory(git_dir: Path, paths: list[str]) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for path in paths:
        if not path.startswith("src/app/") or not path.endswith("/route.ts"):
            continue
        payload = _blob(git_dir, path)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        receipt = _receipt(git_dir, path)
        for method in sorted(set(METHOD_EXPORT_RE.findall(text))):
            routes.append({"method": method, "path": _route_from_source_path(path), **receipt})
    routes.sort(key=lambda row: (row["path"], row["method"], row["source_path"]))
    return routes


def _rewrite_inventory(git_dir: Path) -> list[dict[str, str]]:
    payload = _blob(git_dir, REWRITE_PATH)
    text = payload.decode("utf-8")
    receipt = _receipt(git_dir, REWRITE_PATH)
    pairs = re.findall(
        r"source:\s*[\"']([^\"']+)[\"']\s*,\s*destination:\s*[\"']([^\"']+)[\"']",
        text,
    )
    rows = [
        {"source": source, "destination": destination, "relationship": "rewrite", **receipt}
        for source, destination in pairs
    ]
    return sorted(rows, key=lambda row: (row["source"], row["destination"]))


def _registered_tool_inventory(git_dir: Path, paths: set[str]) -> list[dict[str, Any]]:
    owners: dict[str, list[dict[str, str]]] = {}
    for path in TOOL_OWNER_PATHS:
        if path not in paths:
            raise InventoryError("inventory_tool_owner_missing")
        payload = _blob(git_dir, path)
        text = payload.decode("utf-8")
        names = set(DIRECT_TOOL_RE.findall(text)) | set(NAMED_TOOL_RE.findall(text))
        receipt = _receipt(git_dir, path)
        for name in sorted(names):
            owners.setdefault(name, []).append(receipt)
    return [
        {"name": name, "owners": sorted(owners[name], key=lambda row: row["source_path"])}
        for name in sorted(owners)
    ]


def _normalize_claim_path(path: str) -> str:
    path = path.split("?", 1)[0].rstrip("`.,;:")
    path = re.sub(r"\[\.\.\.([^\]]+)\]", r"{\1*}", path)
    path = re.sub(r"\[([^\]]+)\]", r"{\1}", path)
    path = re.sub(r"<([^>]+)>", r"{\1}", path)
    return path


def _retained_claims(localsetup_root: Path) -> list[dict[str, str]]:
    claims: set[tuple[str, str, str, str]] = set()
    resolved_root = localsetup_root.resolve(strict=True)
    localsetup_ls = localsetup_root / "ls"
    if localsetup_ls.is_symlink():
        raise InventoryError("inventory_retained_ls_symlink")
    skills_root = localsetup_ls / "skills"
    if skills_root.is_symlink():
        raise InventoryError("inventory_retained_skills_symlink")
    for package in RETAINED_PACKAGES:
        package_root = skills_root / package
        if package_root.is_symlink():
            raise InventoryError("inventory_retained_package_symlink")
        if not package_root.is_dir():
            raise InventoryError("inventory_retained_package_missing")
        for path in sorted(package_root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix not in CLAIM_SUFFIXES:
                continue
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, RuntimeError, ValueError):
                raise InventoryError("inventory_retained_claim_outside_root") from None
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise InventoryError("inventory_retained_claim_unreadable") from None
            relative = path.relative_to(localsetup_root).as_posix()
            for methods, route in CLAIM_ENDPOINT_RE.findall(text):
                for method in methods.split("/"):
                    claims.add((package, "endpoint", f"{method} {_normalize_claim_path(route)}", relative))
            for route, methods in CLAIM_ENDPOINT_FIRST_TABLE_RE.findall(text):
                for method in methods.split("/"):
                    claims.add((package, "endpoint", f"{method} {_normalize_claim_path(route)}", relative))
            for methods, route in CLAIM_METHOD_FIRST_TABLE_RE.findall(text):
                for method in methods.split("/"):
                    claims.add((package, "endpoint", f"{method} {_normalize_claim_path(route)}", relative))
            for name in CLAIM_TOOL_RE.findall(text):
                if name.count("_") >= 2:
                    claims.add((package, "tool", name, relative))
    return [
        {"package": package, "kind": kind, "claim": claim, "source_path": source_path}
        for package, kind, claim, source_path in sorted(claims)
    ]


def _rewrite_target(path: str, rewrites: list[dict[str, str]]) -> str | None:
    for row in rewrites:
        source = row["source"]
        destination = row["destination"]
        if source == path:
            return destination
        if source.endswith(":path*"):
            prefix = source.removesuffix(":path*")
            if path.startswith(prefix):
                return destination.removesuffix(":path*") + path[len(prefix) :]
    return None


def _suffix_wildcard_targets(
    method: str,
    pattern: str,
    route_endpoints: set[tuple[str, str]],
) -> list[str]:
    """Resolve one documented suffix wildcard to exact source route operations.

    This records documentation provenance only; it never establishes endpoint
    access, entitlement, or a permitted runtime invocation.
    """
    if not pattern.endswith("*"):
        return []
    prefix = pattern.removesuffix("*")
    if not prefix:
        return []

    def matches(candidate: str) -> bool:
        if prefix.endswith("/"):
            return candidate.startswith(prefix)
        return candidate == prefix or candidate.startswith(f"{prefix}/")

    return [
        f"{method} {candidate}"
        for candidate_method, candidate in sorted(route_endpoints)
        if candidate_method == method and matches(candidate)
    ]


def _resolve_claims(
    git_dir: Path,
    claims: list[dict[str, str]],
    routes: list[dict[str, str]],
    openapi: list[dict[str, str]],
    rewrites: list[dict[str, str]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    endpoints = {(row["method"], row["path"]) for row in [*routes, *openapi]}
    route_endpoints = {(row["method"], row["path"]) for row in routes}
    registered_tools = {row["name"] for row in tools}
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    for claim in claims:
        key = f"{claim['kind']}:{claim['claim']}"
        resolution: dict[str, Any] | None = None
        if claim["kind"] == "tool" and claim["claim"] in registered_tools:
            resolution = {"status": "registered", "target": claim["claim"]}
        elif claim["kind"] == "endpoint":
            method, path = claim["claim"].split(" ", 1)
            if (method, path) in endpoints:
                resolution = {"status": "registered", "target": claim["claim"]}
            else:
                target = _rewrite_target(path, rewrites)
                if target and (method, target) in endpoints:
                    resolution = {
                        "status": "compatible-rewrite",
                        "target": f"{method} {target}",
                    }
                elif (method, path) in SUPPORTED_SUFFIX_WILDCARD_CLAIMS:
                    wildcard_pattern = target or path
                    wildcard_targets = _suffix_wildcard_targets(
                        method, wildcard_pattern, route_endpoints
                    )
                    if wildcard_targets:
                        resolution = {
                            "status": (
                                "compatible-rewrite-wildcard"
                                if target
                                else "registered-wildcard"
                            ),
                            "targets": wildcard_targets,
                        }
        if resolution is None and key in CLAIM_EXCEPTIONS:
            exception = CLAIM_EXCEPTIONS[key]
            evidence = exception["source_evidence"]
            resolution = {
                "status": "source-backed-exception",
                "reason": exception["reason"],
                "evidence": _receipt(git_dir, evidence),
            }
        if resolution is None:
            unresolved.append(claim)
        else:
            resolved.append({**claim, **resolution})
    return {"resolved": resolved, "unresolved": unresolved}


def build_inventory(git_dir: Path, localsetup_root: Path | None = None) -> dict[str, Any]:
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise InventoryError("inventory_mirror_missing")
    source = _source_provenance(git_dir)
    if localsetup_root is None:
        raise InventoryError("inventory_localsetup_root_required")
    if (
        not localsetup_root.is_dir()
        or localsetup_root.is_symlink()
        or not (localsetup_root / "ls").is_dir()
    ):
        raise InventoryError("inventory_localsetup_root_invalid")
    paths = _tracked_paths(git_dir)
    path_set = set(paths)
    skills = _skill_inventory(git_dir, paths)
    openapi = _openapi_inventory(git_dir)
    routes = _route_inventory(git_dir, paths)
    rewrites = _rewrite_inventory(git_dir)
    tools = _registered_tool_inventory(git_dir, path_set)
    claims = _resolve_claims(
        git_dir,
        _retained_claims(localsetup_root),
        routes,
        openapi,
        rewrites,
        tools,
    )
    report = {
        "schema_version": 3,
        "source": source,
        "skills": skills,
        "route_handlers": routes,
        "openapi_operations": openapi,
        "compatibility_rewrites": rewrites,
        "registered_tools": tools,
        "retained_claims": claims,
    }
    report["digests"] = {
        key: _sha256_json(report[key])
        for key in (
            "skills",
            "route_handlers",
            "openapi_operations",
            "compatibility_rewrites",
            "registered_tools",
            "retained_claims",
        )
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract immutable OmniRoute skills, routes, OpenAPI, tools, and retained claims."
    )
    parser.add_argument("--git-dir", type=Path, required=True)
    parser.add_argument("--localsetup-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_inventory(args.git_dir, args.localsetup_root)
    except InventoryError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["retained_claims"]["unresolved"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
