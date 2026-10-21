"""Validation, config, mapping, reference, and redaction helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - checked by caller in normal use
    yaml = None

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:+-]{0,191}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SECRET_REF_RE = re.compile(r"\{\{secret:([^}:]+):([^}]+)\}\}")
SECRET_ID_LINE_RE = re.compile(r"^Secret ID:\s*([A-Za-z0-9@._:+-]+)\s*$")
SECRET_URI_RE = re.compile(
    r"^secret://localsetup/([a-z0-9_-]+)/([a-z0-9_-]+)/([a-z0-9][a-z0-9._-]{0,127})(?:#field=([A-Za-z0-9_.-]+))?$"
)
SENSITIVE_KEYS = {"password", "token", "secret", "key", "passphrase", "private_key", "Password"}
SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "key_material",
    "passphrase",
)
ENV_PREFIX = "LOCALSETUP_SECRETS_"
SAFE_WRITE_FIELDS = {"username", "password", "url", "notes", "UserName", "Password", "URL", "Notes", "title", "path"}


class CliError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class ResolvedConfig:
    repo_root: Path
    config: dict[str, Any]
    map_path: Path | None
    sources: list[str]


def clean_text(value: Any, *, label: str, max_len: int = 4096, required: bool = False) -> str:
    if value is None:
        if required:
            raise CliError("invalid_input", f"{label} is required")
        return ""
    if not isinstance(value, str):
        raise CliError("invalid_input", f"{label} must be a string")
    value = CONTROL_RE.sub(" ", value).strip()
    if required and not value:
        raise CliError("invalid_input", f"{label} is required")
    if len(value) > max_len:
        raise CliError("invalid_input", f"{label} exceeds {max_len} characters")
    return value


def validate_id(secret_id: str) -> str:
    secret_id = clean_text(secret_id, label="secret id", max_len=128, required=True).lower()
    if not ID_RE.fullmatch(secret_id):
        raise CliError("invalid_secret_id", f"Invalid canonical secret id: {secret_id}")
    return secret_id


def validate_alias(alias: str) -> str:
    alias = clean_text(alias, label="alias", max_len=192, required=True)
    if ID_RE.fullmatch(alias.lower()):
        return alias.lower()
    if not ALIAS_RE.fullmatch(alias):
        raise CliError("invalid_alias", f"Invalid alias: {alias}")
    return alias


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return key in SENSITIVE_KEYS or any(part in lowered for part in SECRET_KEY_PARTS)


def reject_secret_values(value: Any, *, source: str, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_s = str(key)
            next_path = f"{path}.{key_s}" if path else key_s
            if is_sensitive_key(key_s) and item not in (None, "", "prompt"):
                raise CliError("secret_value_in_file", f"{source} contains secret-like value at {next_path}")
            reject_secret_values(item, source=source, path=next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_values(item, source=source, path=f"{path}[{index}]")


def read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise CliError("missing_dependency", "PyYAML is required")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise CliError("read_failed", f"Could not read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CliError("invalid_yaml", f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise CliError("invalid_yaml", f"{path} must contain a YAML mapping")
    reject_secret_values(data, source=str(path))
    return data


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for path in (current, *current.parents):
        if (path / "ls").is_dir():
            return path
    return current


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def resolve_map_path(repo_root: Path, config: dict[str, Any]) -> Path | None:
    if config.get("map_path"):
        path = Path(str(config["map_path"])).expanduser()
        return path if path.is_absolute() else (repo_root / path).resolve()
    repo_map = repo_root / ".localsetup" / "secrets" / "map.yaml"
    if repo_map.is_file():
        return repo_map
    legacy_maps = sorted((repo_root / "secrets").glob("*-secrets-map.yaml")) if (repo_root / "secrets").is_dir() else []
    if len(legacy_maps) == 1:
        return legacy_maps[0]
    if len(legacy_maps) > 1:
        return None
    global_map = Path.home() / ".local" / "share" / "localsetup" / "secrets" / "maps" / "default.yaml"
    return global_map if global_map.is_file() else None


def resolve_config(args) -> ResolvedConfig:
    repo_root = Path(args.repo_root).expanduser().resolve() if getattr(args, "repo_root", None) else find_repo_root(Path.cwd())
    config: dict[str, Any] = {
        "backend": "keepassxc",
        "scope": "repo",
        "profile": "default",
        "keepassxc_binary": "keepassxc-cli",
    }
    sources: list[str] = []
    candidate_configs = [
        Path.home() / ".config" / "localsetup" / "secrets" / "config.yaml",
        repo_root / "secrets" / "keepass-config.yaml",
        repo_root / ".localsetup" / "secrets" / "config.yaml",
    ]
    for path in candidate_configs:
        if path.is_file():
            config = deep_merge(config, read_yaml(path))
            sources.append(str(path))
    env_map = {
        "BACKEND": "backend",
        "CONFIG": "config_path",
        "MAP": "map_path",
        "PROFILE": "profile",
        "SCOPE": "scope",
        "DATABASE": "database",
        "KEEPASSXC_BINARY": "keepassxc_binary",
        "FAKE_STORE": "fake_store",
    }
    for env_name, config_name in env_map.items():
        value = os.environ.get(f"{ENV_PREFIX}{env_name}")
        if value:
            config[config_name] = clean_text(value, label=f"{ENV_PREFIX}{env_name}")
            sources.append(f"env:{ENV_PREFIX}{env_name}")
    if getattr(args, "config", None):
        path = Path(args.config).expanduser().resolve()
        config = deep_merge(config, read_yaml(path))
        sources.append(str(path))
    for attr, key in (
        ("backend", "backend"),
        ("profile", "profile"),
        ("scope", "scope"),
        ("database", "database"),
        ("map", "map_path"),
        ("keepassxc_binary", "keepassxc_binary"),
        ("fake_store", "fake_store"),
    ):
        value = getattr(args, attr, None)
        if value:
            config[key] = clean_text(value, label=attr)
            sources.append(f"cli:{attr}")
    return ResolvedConfig(repo_root=repo_root, config=config, map_path=resolve_map_path(repo_root, config), sources=sources)


def flatten_entries(entries: Any, prefix: tuple[str, ...] = ()) -> dict[str, dict[str, Any]]:
    flat: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, dict):
        raise CliError("invalid_map", "entries must be a mapping")
    for key, value in entries.items():
        part = clean_text(str(key), label="entry key", max_len=128, required=True).lower()
        current = (*prefix, part)
        if isinstance(value, dict) and ("path" in value or "service_type" in value or "username" in value):
            secret_id = validate_id(".".join(current))
            item = dict(value)
            item["id"] = secret_id
            flat[secret_id] = item
        elif isinstance(value, dict):
            flat.update(flatten_entries(value, current))
        else:
            raise CliError("invalid_map", f"entry {'.'.join(current)} must be a mapping")
    return flat


def flatten_aliases(aliases: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if aliases in (None, {}):
        return result
    if not isinstance(aliases, dict):
        raise CliError("invalid_map", "aliases must be a mapping")
    for key, value in aliases.items():
        if isinstance(value, str):
            alias = validate_alias(str(key))
            target = validate_id(value)
            if alias in result and result[alias] != target:
                raise CliError("ambiguous_alias", f"Alias {alias} maps to multiple ids")
            result[alias] = target
        elif isinstance(value, dict):
            nested = flatten_aliases(value)
            for alias, target in nested.items():
                if alias in result and result[alias] != target:
                    raise CliError("ambiguous_alias", f"Alias {alias} maps to multiple ids")
                result[alias] = target
        else:
            raise CliError("invalid_map", f"Alias {key} must map to a string or mapping")
    return result


def load_map(config: ResolvedConfig) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    if not config.map_path:
        return {}, {}, []
    data = read_yaml(config.map_path)
    entries = flatten_entries(data.get("entries", {}))
    aliases = flatten_aliases(data.get("aliases", {}))
    return entries, aliases, [str(config.map_path)]


def resolve_secret_id(raw: str, aliases: dict[str, str]) -> str:
    alias = validate_alias(raw)
    lowered = alias.lower()
    if lowered in aliases:
        return aliases[lowered]
    if alias in aliases:
        return aliases[alias]
    return validate_id(lowered)


def parse_reference(raw: str) -> dict[str, str]:
    text = clean_text(raw, label="reference", max_len=512, required=True)
    match = SECRET_ID_LINE_RE.fullmatch(text)
    if match:
        return {"id": validate_alias(match.group(1)), "field": "password", "type": "secret-id-line"}
    match = SECRET_REF_RE.fullmatch(text)
    if match:
        return {"id": validate_alias(match.group(1)), "field": clean_text(match.group(2), label="field", required=True), "type": "template"}
    match = SECRET_URI_RE.fullmatch(text)
    if match:
        return {
            "scope": match.group(1),
            "profile": match.group(2),
            "id": validate_id(match.group(3)),
            "field": match.group(4) or "password",
            "type": "uri",
        }
    return {"id": validate_alias(text), "field": "password", "type": "id"}


def redact(value: Any, redactions: list[str], path: str = "") -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_s = str(key)
            next_path = f"{path}.{key_s}" if path else key_s
            if is_sensitive_key(key_s):
                if item not in (None, ""):
                    redactions.append(next_path)
                result[key] = "<redacted>" if item not in (None, "") else item
            else:
                result[key] = redact(item, redactions, next_path)
        return result
    if isinstance(value, list):
        return [redact(item, redactions, f"{path}[{index}]") for index, item in enumerate(value)]
    return value


def envelope(
    command: str,
    data: Any = None,
    *,
    ok: bool = True,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
    show_sensitive: bool = False,
    sensitive: bool = False,
) -> dict[str, Any]:
    redactions: list[str] = []
    rendered = data if show_sensitive else redact(data, redactions)
    return {
        "ok": ok,
        "command": command,
        "data": rendered,
        "warnings": warnings or [],
        "errors": errors or [],
        "sources": sources or [],
        "sensitive": bool(sensitive and show_sensitive),
        "redactions": sorted(set(redactions)),
    }


class KeePassXCBackend:
    name = "keepassxc"

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.path = shutil.which(binary)
        if not self.path:
            raise CliError("missing_backend", f"{binary} not found on PATH")

    def _run(self, argv: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.path, *argv],
            shell=False,
            text=True,
            input=input_text,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def info(self) -> dict[str, Any]:
        result = self._run(["--version"])
        version_text = "\n".join([result.stdout, result.stderr]).strip()
        return {"backend": self.name, "binary": self.path, "version": version_text}

    def resolve(self, secret_id: str, mapping: dict[str, Any]) -> dict[str, Any]:
        raise CliError(
            "interactive_backend_required",
            "KeePassXC reads require an interactive vault operation; use mapping validation or --backend fake for tests",
            details={"id": secret_id, "path": (mapping or {}).get("path")},
        )

    def list_entries(self) -> list[dict[str, Any]]:
        raise CliError("interactive_backend_required", "KeePassXC listing requires an interactive vault operation")

    def search(self, query: str) -> list[dict[str, Any]]:
        raise CliError("interactive_backend_required", "KeePassXC search requires an interactive vault operation")

    def ensure(self, items: list[dict[str, Any]], *, apply: bool = False, rotate: bool = False) -> dict[str, Any]:
        raise CliError("interactive_backend_required", "KeePassXC writes require an interactive vault operation")

    def set_field(self, secret_id: str, field: str, value: str, *, apply: bool = False) -> dict[str, Any]:
        if field not in SAFE_WRITE_FIELDS:
            raise CliError("unsupported_field", f"unsupported_field: {field}")
        raise CliError("interactive_backend_required", "KeePassXC field writes require an interactive vault operation")

    def delete(self, secret_id: str, *, apply: bool = False) -> dict[str, Any]:
        raise CliError("interactive_backend_required", "KeePassXC deletes require an interactive vault operation")


def backend_for(config: ResolvedConfig, fake_backend_type: type | None = None):
    backend = clean_text(config.config.get("backend", "keepassxc"), label="backend").lower()
    if backend == "fake":
        if fake_backend_type is None:
            raise CliError("missing_backend", "Fake backend is not available")
        store = config.config.get("fake_store")
        store_path = Path(str(store)).expanduser().resolve() if store else None
        return fake_backend_type(store_path)
    if backend == "keepassxc":
        return KeePassXCBackend(str(config.config.get("keepassxc_binary", "keepassxc-cli")))
    raise CliError("unsupported_backend", f"Unsupported backend: {backend}")
