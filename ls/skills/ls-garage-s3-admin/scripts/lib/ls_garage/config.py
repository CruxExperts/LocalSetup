"""Separate explicit S3/admin endpoints and protected credential references."""
from __future__ import annotations

import json
import os
import re
import stat
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .reporting import ToolError


def reference(value: Any) -> None:
    if not isinstance(value, dict) or set(value) not in ({"env"}, {"file"}, {"file", "field"}) or any(not isinstance(item, str) or not item or "\x00" in item for item in value.values()):
        raise ToolError("credential_reference_invalid", "use an explicit environment or protected-file reference, with an optional JSON field selector")


def resolve(value: dict[str, str]) -> str:
    reference(value)
    if "env" in value:
        result = os.environ.get(value["env"])
        if not result:
            raise ToolError("credential_unavailable", "selected credential environment variable is unset")
        return result
    path = Path(value["file"])
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ToolError("credential_file_unsafe", "protected file path must not contain symlinks")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
                raise ToolError("credential_file_unsafe", "protected file must be an owned regular mode0600 file")
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ToolError("credential_file_unsafe", "protected file exceeds64KiB")
        result = raw.decode("utf-8").rstrip("\r\n")
        if "field" in value:
            parsed = json.loads(result)
            result = parsed.get(value["field"]) if isinstance(parsed, dict) else None
        if not isinstance(result, str) or not result:
            raise ToolError("credential_unavailable", "protected file or selected JSON field is empty or invalid")
        return result
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("credential_unavailable", "protected file cannot be read") from exc


def endpoint(value: Any, allow_http: bool = False) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ToolError("configuration_invalid", "endpoint must be an explicit URL without whitespace/control characters")
    try:
        parsed = urlparse(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ToolError("unsafe_endpoint", "endpoint URL is malformed") from exc
    if not host or any(char in host for char in "\\%") or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or port is not None and not 1 <= port <= 65535:
        raise ToolError("unsafe_endpoint", "endpoint must have an explicit host and no credentials, query or path prefix")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and allow_http and host in {"127.0.0.1", "::1"}):
        raise ToolError("unsafe_endpoint", "HTTPS is required; HTTP needs explicit opt-in and literal127.0.0.1 or::1")
    return value.rstrip("/")


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        raise ToolError("configuration_missing", "--apply requires --config")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ToolError("configuration_invalid", "duplicate configuration field")
            result[key] = value
        return result
    try:
        with Path(path).open("rb") as source:
            raw = source.read(1024**2 + 1)
        if len(raw) > 1024**2:
            raise ToolError("configuration_invalid", "configuration exceeds1MiB")
        config = json.loads(raw, object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise ToolError("configuration_invalid", "could not read JSON configuration") from exc
    if not isinstance(config, dict) or set(config) - {"s3", "admin", "request_budget_seconds", "allow_loopback_http"}:
        raise ToolError("configuration_invalid", "unsupported configuration fields")
    budget = config.get("request_budget_seconds", 300)
    if type(budget) is not int or not 1 <= budget <= 300 or type(config.get("allow_loopback_http", False)) is not bool:
        raise ToolError("configuration_invalid", "budget must be1..300 seconds and loopback opt-in must be boolean")
    for name in ("s3", "admin"):
        if name not in config:
            continue
        section = config[name]
        required = {"endpoint", "region", "access_key_id", "secret_access_key"} if name == "s3" else {"endpoint", "token"}
        optional = {"ca_bundle", "addressing_style"} if name == "s3" else {"ca_bundle"}
        if not isinstance(section, dict) or not required <= set(section) or set(section) - required - optional:
            raise ToolError("configuration_invalid", f"{name} section has missing or unsupported fields")
        endpoint(section["endpoint"], config.get("allow_loopback_http", False))
        for field in ({"access_key_id", "secret_access_key"} if name == "s3" else {"token"}):
            reference(section[field])
        if name == "s3" and (not isinstance(section["region"], str) or re.fullmatch(r"[A-Za-z0-9_-]+", section["region"]) is None):
            raise ToolError("configuration_invalid", "configured signing region must be a nonempty identifier")
        if name == "s3" and section.get("addressing_style", "path") != "path":
            raise ToolError("configuration_invalid", "Garage S3 supports only path addressing_style")
        if "ca_bundle" in section and (not isinstance(section["ca_bundle"], str) or not Path(section["ca_bundle"]).is_file()):
            raise ToolError("configuration_invalid", "ca_bundle must name a readable PEM file; verification cannot be disabled")
        if "ca_bundle" in section:
            try:
                ssl.create_default_context(cafile=section["ca_bundle"])
            except (OSError, ValueError) as exc:
                raise ToolError("configuration_invalid", "ca_bundle must contain usable trusted PEM certificates") from exc
    return config


def s3_values(config: dict[str, Any]) -> dict[str, Any]:
    if "s3" not in config:
        raise ToolError("configuration_missing", "separate S3 configuration is required")
    section = config["s3"]
    return {"endpoint_url": endpoint(section["endpoint"], config.get("allow_loopback_http", False)), "region_name": section["region"], "aws_access_key_id": resolve(section["access_key_id"]), "aws_secret_access_key": resolve(section["secret_access_key"]), "verify": section.get("ca_bundle", True), "request_budget_seconds": config.get("request_budget_seconds", 300)}


def admin_values(config: dict[str, Any]) -> dict[str, Any]:
    if "admin" not in config:
        raise ToolError("configuration_missing", "separate admin configuration is required")
    section = config["admin"]
    return {"endpoint": endpoint(section["endpoint"], config.get("allow_loopback_http", False)), "token": resolve(section["token"]), "ca_bundle": section.get("ca_bundle"), "request_budget_seconds": config.get("request_budget_seconds", 300)}
