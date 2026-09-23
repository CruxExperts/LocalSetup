import argparse
from pathlib import Path
from typing import Any

from .common import (
    GLOBAL_CONFIG_REL,
    GLOBAL_DB_REL,
    REPO_CONFIG_REL,
    REPO_DB_REL,
    ContextIndexError,
    Runtime,
    default_config,
    slugify,
    yaml,
)

def read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ContextIndexError("MISSING_DEPENDENCY", "PyYAML is required for context-index config.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ContextIndexError("INVALID_CONFIG", f"Config root must be a mapping: {path}")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(repo_root: Path, home: Path) -> tuple[dict[str, Any], list[str]]:
    config = default_config(repo_root, home)
    loaded: list[str] = []
    global_path = home / GLOBAL_CONFIG_REL
    repo_path = repo_root / REPO_CONFIG_REL
    repo_memory_uuid_configured = False
    for path in (global_path, repo_path):
        if path.is_file():
            data = read_yaml(path)
            config = deep_merge(config, data)
            loaded.append(str(path))
            if path == repo_path:
                context_index = data.get("context_index")
                identity = context_index.get("identity") if isinstance(context_index, dict) else None
                repo_memory_uuid_configured = isinstance(identity, dict) and "memory_uuid" in identity
    if not repo_memory_uuid_configured:
        identity = config["context_index"].get("identity")
        if isinstance(identity, dict):
            identity.pop("memory_uuid", None)
    return config, loaded


def context_for(config: dict[str, Any], repo_root: Path, scope: str) -> dict[str, str]:
    ci = config["context_index"]
    ident = ci.get("identity", {})
    if scope == "framework":
        tenant, namespace, corpus = "local", "framework", "localsetup"
    elif scope == "global":
        raise ContextIndexError("UNSUPPORTED_SCOPE", "context-index global scope has been removed")
    else:
        tenant = str(ident.get("tenant_slug") or "local")
        namespace = str(ident.get("namespace_slug") or "repos")
        corpus = str(ident.get("corpus_slug") or slugify(repo_root.name))
    return {
        "tenant_slug": tenant,
        "namespace_slug": namespace,
        "corpus_slug": corpus,
        "scope_slug": scope,
        "context_key": f"{tenant}/{namespace}/{corpus}/{scope}",
    }


def db_path_for(config: dict[str, Any], repo_root: Path, home: Path, scope: str, database: str | None = None) -> Path:
    ci = config["context_index"]
    storage = ci.get("storage", {})
    if database == "global" or scope in {"framework", "global"} or storage.get("mode") in {"global", "central_sqlite"}:
        return Path(str(storage.get("global_database", {}).get("path") or home / GLOBAL_DB_REL)).expanduser()
    return (repo_root / str(storage.get("repo_database", {}).get("path") or REPO_DB_REL)).resolve()


def runtime(args: argparse.Namespace, scope: str | None = None, database: str | None = None) -> Runtime:
    target_root = Path(args.repo).expanduser().resolve()
    source_root = Path(getattr(args, "source_root", None) or target_root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve()
    selected_scope = scope or getattr(args, "scope", "repo") or "repo"
    first_scope = str(selected_scope).split(",")[0].strip() or "repo"
    repo_root = source_root if first_scope == "framework" else target_root
    config, _ = load_config(repo_root, home)
    ctx = context_for(config, repo_root, first_scope)
    db_path = db_path_for(config, repo_root, home, first_scope, database=database)
    rt = Runtime(repo_root, home, config, ctx, db_path, first_scope)
    scope_definition(rt, first_scope)
    return rt


def parse_scopes(value: str | None) -> list[str]:
    scopes = [part.strip() for part in str(value or "repo").split(",") if part.strip()]
    return scopes or ["repo"]


def scope_definition(rt: Runtime, scope: str) -> dict[str, Any]:
    defs = rt.config["context_index"].get("scopes", {}).get("definitions", {})
    if scope not in defs:
        raise ContextIndexError("UNSUPPORTED_SCOPE", f"context-index scope is not configured: {scope}")
    definition = dict(defs[scope])
    if str(definition.get("type", "")) == "global":
        raise ContextIndexError("UNSUPPORTED_SCOPE", "context-index global scope has been removed")
    repo_root = rt.repo_root.resolve()
    for root_value in definition.get("roots") or []:
        root = Path(str(root_value)).expanduser()
        base = root if root.is_absolute() else rt.repo_root / root
        try:
            base.resolve().relative_to(repo_root)
        except ValueError as exc:
            raise ContextIndexError("UNSUPPORTED_SCOPE", f"context-index scope root is outside the repository: {root_value}") from exc
    return definition
