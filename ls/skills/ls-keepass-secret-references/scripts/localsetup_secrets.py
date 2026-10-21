#!/usr/bin/env python3
"""JSON-first LocalSetup KeePass secrets helper."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    if (parent / "lib" / "deps.py").is_file():
        sys.path.insert(0, str(parent / "lib"))
        from deps import require_deps

        require_deps(["yaml"])
        break

try:
    import yaml
except ImportError as exc:  # pragma: no cover - exercised by environments
    raise SystemExit("Missing dependency: PyYAML. Run `uv sync --locked --no-dev` from the LocalSetup source checkout.") from exc

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from fake_vault_backend import FakeVaultBackend, generate_password  # noqa: E402

from localsetup_secrets_support import (  # noqa: E402
    SECRET_REF_RE, SAFE_WRITE_FIELDS, SENSITIVE_KEYS, CliError, KeePassXCBackend,
    ResolvedConfig, backend_for as _backend_for, clean_text, deep_merge, envelope,
    find_repo_root, flatten_aliases, flatten_entries, is_sensitive_key, load_map,
    parse_reference, read_yaml, redact, reject_secret_values, resolve_config,
    resolve_map_path, resolve_secret_id, validate_alias, validate_id,
)


def backend_for(config: ResolvedConfig) -> Any:
    return _backend_for(config, FakeVaultBackend)


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    data = {
        "repo_root": str(config.repo_root),
        "config": config.config,
        "map_path": str(config.map_path) if config.map_path else None,
        "python": sys.version.split()[0],
        "pyyaml": getattr(yaml, "__version__", "unknown"),
    }
    warnings: list[str] = []
    try:
        be = backend_for(config)
        data["backend"] = be.info()
    except CliError as exc:
        warnings.append(exc.message)
        data["backend"] = {"backend": config.config.get("backend"), "available": False}
    return envelope("doctor", data, warnings=warnings, sources=config.sources)


def command_config_show(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    return envelope("config-show", {"config": config.config, "map_path": str(config.map_path) if config.map_path else None}, sources=config.sources)


def command_config_init(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    target = config.repo_root / ".localsetup" / "secrets" / "config.yaml"
    sample = {
        "backend": args.backend or "keepassxc",
        "scope": args.scope or "repo",
        "profile": args.profile or "default",
        "map_path": ".localsetup/secrets/map.yaml",
        "keepassxc_binary": "keepassxc-cli",
    }
    if target.exists() and not args.apply:
        return envelope("config-init", {"path": str(target), "would_write": False, "exists": True}, warnings=["Use --apply to overwrite intentionally"])
    if args.apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(sample, sort_keys=False), encoding="utf-8")
    return envelope("config-init", {"path": str(target), "would_write": not args.apply, "config": sample})


def command_config_validate(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    errors = []
    if config.config.get("backend") not in {"keepassxc", "fake"}:
        errors.append({"code": "unsupported_backend", "message": "backend must be keepassxc or fake"})
    return envelope("config-validate", {"config": config.config}, ok=not errors, errors=errors, sources=config.sources)


def command_map_validate(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    return envelope("map-validate", {"entries": sorted(entries), "aliases": aliases, "count": len(entries)}, sources=config.sources + sources)


def command_list(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, _, sources = load_map(config)
    be = backend_for(config)
    backend_entries = be.list_entries() if hasattr(be, "list_entries") else []
    return envelope("list", {"mapped": entries, "backend_entries": backend_entries}, sources=config.sources + sources)


def command_search(args: argparse.Namespace) -> dict[str, Any]:
    query = clean_text(args.query, label="query", max_len=128, required=True)
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    mapped = {key: value for key, value in entries.items() if query.lower() in key or query.lower() in str(value).lower()}
    be = backend_for(config)
    backend_entries = be.search(query) if hasattr(be, "search") else []
    return envelope("search", {"mapped": mapped, "aliases": aliases, "backend_entries": backend_entries}, sources=config.sources + sources)


def command_reference(args: argparse.Namespace) -> dict[str, Any]:
    parsed = parse_reference(args.reference)
    return envelope("reference", parsed)


def command_resolve(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    parsed = parse_reference(args.id)
    secret_id = resolve_secret_id(parsed["id"], aliases)
    mapping = entries.get(secret_id, {})
    if secret_id not in entries and config.map_path:
        raise CliError("unknown_secret_id", f"{secret_id} is not present in the configured map")
    data = backend_for(config).resolve(secret_id, mapping)
    field = args.field or parsed.get("field")
    if field and field != "all":
        data = {"id": secret_id, field: data.get(field)}
    sensitive = bool(field in SENSITIVE_KEYS or "password" in data or "token" in data)
    if sensitive and not args.show_sensitive:
        data["_hint"] = "Use --show-sensitive to print protected values"
    return envelope("resolve", data, sources=config.sources + sources, show_sensitive=args.show_sensitive, sensitive=sensitive)


def _batch_items(args: argparse.Namespace, entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if args.batch:
        path = Path(args.batch).expanduser().resolve()
        data = read_yaml(path)
        raw_items = data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise CliError("invalid_batch", "batch file must contain a non-empty items list")
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise CliError("invalid_batch", "batch items must be mappings")
            item = dict(raw)
            item["id"] = validate_id(str(item.get("id", "")))
            items.append(item)
        return items
    secret_id = validate_id(args.id)
    item = dict(entries.get(secret_id, {}))
    item["id"] = secret_id
    if args.username:
        item["username"] = clean_text(args.username, label="username", max_len=256)
    return [item]


def command_ensure(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, _, sources = load_map(config)
    items = _batch_items(args, entries)
    result = backend_for(config).ensure(items, apply=args.apply, rotate=args.rotate)
    return envelope("ensure", result, warnings=[] if args.apply else ["dry-run only; pass --apply to write"], sources=config.sources + sources)


def command_set(args: argparse.Namespace) -> dict[str, Any]:
    field = clean_text(args.field, label="field", max_len=64, required=True)
    if field not in SAFE_WRITE_FIELDS:
        raise CliError("unsupported_field", f"unsupported_field: {field}")
    value = sys.stdin.read() if args.stdin else args.value
    value = clean_text(value, label="value", max_len=65536, required=True)
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    secret_id = resolve_secret_id(args.id, aliases)
    if config.map_path and secret_id not in entries:
        raise CliError("unknown_secret_id", f"{secret_id} is not present in the configured map")
    result = backend_for(config).set_field(secret_id, field, value, apply=args.apply)
    return envelope("set", result, warnings=[] if args.apply else ["dry-run only; pass --apply to write"], sources=config.sources + sources)


def command_rotate(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    secret_id = resolve_secret_id(args.id, aliases)
    item = dict(entries.get(secret_id, {}))
    item["id"] = secret_id
    item["password"] = generate_password()
    result = backend_for(config).ensure([item], apply=args.apply, rotate=True)
    return envelope("rotate", result, warnings=[] if args.apply else ["dry-run only; pass --apply to write"], sources=config.sources + sources)


def command_delete(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    _, aliases, sources = load_map(config)
    secret_id = resolve_secret_id(args.id, aliases)
    result = backend_for(config).delete(secret_id, apply=args.apply)
    return envelope("delete", result, warnings=[] if args.apply else ["dry-run only; pass --apply to write"], sources=config.sources + sources)


def command_audit(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    duplicate_paths: dict[str, list[str]] = {}
    for secret_id, item in entries.items():
        path = str(item.get("path", ""))
        if path:
            duplicate_paths.setdefault(path, []).append(secret_id)
    findings = [
        {"code": "duplicate_path", "path": path, "ids": ids}
        for path, ids in duplicate_paths.items()
        if len(ids) > 1
    ]
    return envelope("audit", {"entries": len(entries), "aliases": len(aliases), "findings": findings}, ok=not findings, sources=config.sources + sources)


def command_export_env(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    pairs: dict[str, str] = {}
    be = backend_for(config)
    for spec in args.vars:
        if "=" not in spec:
            raise CliError("invalid_input", "vars must use NAME=secret-id:field")
        name, ref = spec.split("=", 1)
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name):
            raise CliError("invalid_input", f"Invalid environment variable name: {name}")
        secret_part, _, field = ref.partition(":")
        secret_id = resolve_secret_id(secret_part, aliases)
        data = be.resolve(secret_id, entries.get(secret_id, {}))
        pairs[name] = str(data.get(field or "password", ""))
    return envelope("export-env", {"env": pairs}, sources=config.sources + sources, show_sensitive=args.show_sensitive, sensitive=True)


def command_render_template(args: argparse.Namespace) -> dict[str, Any]:
    template_path = Path(args.template).expanduser().resolve()
    text = template_path.read_text(encoding="utf-8", errors="replace")
    config = resolve_config(args)
    entries, aliases, sources = load_map(config)
    be = backend_for(config)
    used: list[str] = []

    def repl(match: re.Match[str]) -> str:
        secret_id = resolve_secret_id(match.group(1), aliases)
        field = match.group(2)
        used.append(secret_id)
        data = be.resolve(secret_id, entries.get(secret_id, {}))
        return str(data.get(field, ""))

    rendered = SECRET_REF_RE.sub(repl, text)
    output = getattr(args, "output", None)
    if output:
        if not args.show_sensitive:
            raise CliError("sensitive_output_requires_opt_in", "render-template --output requires --show-sensitive")
        output_path = Path(output).expanduser().resolve()
        if is_tracked_path(config.repo_root, output_path) and not args.allow_tracked_output:
            raise CliError("tracked_output_refused", f"Refusing to write sensitive rendered output to tracked path: {output_path}")
        if is_path_under(config.repo_root, output_path) and not is_ignored_path(config.repo_root, output_path) and not args.allow_unignored_output:
            raise CliError("tracked_output_refused", f"Refusing to write sensitive rendered output to non-ignored repo path: {output_path}")
        if args.apply:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered, encoding="utf-8")
        data = {"output": str(output_path), "would_write": not args.apply, "used": sorted(set(used))}
    else:
        data = {"rendered": rendered if args.show_sensitive else "<redacted>", "used": sorted(set(used))}
    return envelope("render-template", data, sources=config.sources + sources, show_sensitive=args.show_sensitive, sensitive=True)


def command_vault_info(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    return envelope("vault-info", backend_for(config).info(), sources=config.sources)


def command_vault_init(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    return envelope("vault-init", {"backend": config.config.get("backend"), "would_create": not args.apply}, warnings=[] if args.apply else ["dry-run only; real KeePassXC vault creation is intentionally not automated in v1"], sources=config.sources)


def command_vault_backup(args: argparse.Namespace) -> dict[str, Any]:
    config = resolve_config(args)
    database = config.config.get("database")
    if not database:
        return envelope("vault-backup", {"database": None, "would_copy": False}, warnings=["No database path configured"], sources=config.sources)
    src = Path(str(database)).expanduser()
    if not src.is_absolute():
        src = config.repo_root / src
    target = Path(args.output).expanduser().resolve() if args.output else src.with_suffix(src.suffix + ".bak")
    if is_path_under(config.repo_root, target) and not is_ignored_path(config.repo_root, target):
        raise CliError("tracked_output_refused", f"Refusing to write vault backup to non-ignored repo path: {target}")
    if args.apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return envelope("vault-backup", {"source": str(src), "target": str(target), "would_copy": not args.apply}, warnings=[] if args.apply else ["dry-run only; pass --apply to copy"], sources=config.sources)


SCHEMAS: dict[str, dict[str, Any]] = {
    "config": {"type": "object", "required": ["backend"], "properties": {"backend": {"enum": ["keepassxc", "fake"]}, "map_path": {"type": "string"}}},
    "map": {"type": "object", "required": ["entries"], "properties": {"entries": {"type": "object"}, "aliases": {"type": "object"}}},
    "reference": {"oneOf": ["Secret ID: <id>", "{{secret:<id>:<field>}}", "secret://localsetup/<scope>/<profile>/<id>#field=<field>"]},
    "batch": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array"}}},
    "command-result": {"required": ["ok", "command", "data", "warnings", "errors", "sources", "sensitive", "redactions"]},
}


def command_schema_dump(args: argparse.Namespace) -> dict[str, Any]:
    name = clean_text(args.schema, label="schema", max_len=64, required=True)
    if name not in SCHEMAS:
        raise CliError("unknown_schema", f"Unknown schema: {name}")
    return envelope("schema-dump", {"schema": name, "contract": SCHEMAS[name]})


def is_tracked_path(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(rel)],
        cwd=repo_root,
        shell=False,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


def is_path_under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_ignored_path(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(rel)],
        cwd=repo_root,
        shell=False,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return result.returncode == 0


COMMANDS = {
    "doctor": command_doctor,
    "config-show": command_config_show,
    "config-init": command_config_init,
    "config-validate": command_config_validate,
    "map-validate": command_map_validate,
    "list": command_list,
    "search": command_search,
    "reference": command_reference,
    "resolve": command_resolve,
    "ensure": command_ensure,
    "set": command_set,
    "rotate": command_rotate,
    "delete": command_delete,
    "audit": command_audit,
    "export-env": command_export_env,
    "render-template": command_render_template,
    "vault-info": command_vault_info,
    "vault-init": command_vault_init,
    "vault-backup": command_vault_backup,
    "schema-dump": command_schema_dump,
}


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "human"), default="json")
    parser.add_argument("--repo-root")
    parser.add_argument("--config")
    parser.add_argument("--map")
    parser.add_argument("--backend", choices=("keepassxc", "fake"))
    parser.add_argument("--profile")
    parser.add_argument("--scope")
    parser.add_argument("--database")
    parser.add_argument("--keepassxc-binary", default=None)
    parser.add_argument("--fake-store")
    parser.add_argument("--show-sensitive", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        p = sub.add_parser(name)
        add_common(p)
        if name in {"search"}:
            p.add_argument("query")
        if name in {"reference"}:
            p.add_argument("reference")
        if name in {"resolve", "rotate", "delete"}:
            p.add_argument("id")
        if name in {"rotate", "delete"}:
            p.add_argument("--apply", action="store_true")
        if name == "resolve":
            p.add_argument("--field", default=None)
        if name == "ensure":
            p.add_argument("id", nargs="?")
            p.add_argument("--batch")
            p.add_argument("--username")
            p.add_argument("--rotate", action="store_true")
            p.add_argument("--apply", action="store_true")
        if name == "set":
            p.add_argument("id")
            p.add_argument("field")
            p.add_argument("value", nargs="?")
            p.add_argument("--stdin", action="store_true")
            p.add_argument("--apply", action="store_true")
        if name in {"config-init", "vault-init"}:
            p.add_argument("--apply", action="store_true")
        if name == "vault-backup":
            p.add_argument("--output")
            p.add_argument("--apply", action="store_true")
        if name == "export-env":
            p.add_argument("vars", nargs="+")
        if name == "render-template":
            p.add_argument("template")
            p.add_argument("--output")
            p.add_argument("--apply", action="store_true")
            p.add_argument("--allow-tracked-output", action="store_true")
            p.add_argument("--allow-unignored-output", action="store_true")
        if name == "schema-dump":
            p.add_argument("--schema", required=True, choices=sorted(SCHEMAS))
    return parser


def print_human(result: dict[str, Any]) -> None:
    print(f"{result['command']}: {'ok' if result['ok'] else 'failed'}")
    if result.get("warnings"):
        for warning in result["warnings"]:
            print(f"warning: {warning}")
    if result.get("errors"):
        for error in result["errors"]:
            print(f"error: {error.get('code')}: {error.get('message')}")
    if result.get("data") is not None:
        print(json.dumps(result["data"], indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except CliError as exc:
        result = envelope(
            args.command,
            exc.details,
            ok=False,
            errors=[{"code": exc.code, "message": clean_text(exc.message, label="error", max_len=1024)}],
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result = envelope(
            args.command,
            None,
            ok=False,
            errors=[{"code": exc.__class__.__name__, "message": clean_text(str(exc), label="error", max_len=1024)}],
        )
    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_human(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
