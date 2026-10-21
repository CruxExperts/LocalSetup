"""Explicit configuration and protected secret-reference resolution."""
from __future__ import annotations

import json
import os
import stat
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .reporting import ToolError


def load_config(path: str | None) -> dict[str, Any]:
    if not path: raise ToolError("configuration_missing", "--apply requires --config")
    try:
        with Path(path).open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ToolError("configuration_invalid", "configuration exceeds 1 MiB")
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ToolError("configuration_invalid", "configuration contains duplicate fields")
                result[key] = item
            return result
        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError("configuration_invalid", "could not read JSON configuration") from exc
    if not isinstance(value, dict) or set(value) - {"s3", "native", "request_budget_seconds"}:
        raise ToolError("configuration_invalid", "configuration contains unsupported fields")
    budget = value.get("request_budget_seconds", 300)
    if type(budget) is not int or not 1 <= budget <= 300:
        raise ToolError("configuration_invalid", "request_budget_seconds must be 1..300")
    if "s3" in value: _check_section(value["s3"], {"endpoint", "region", "access_key_id", "secret_access_key", "session_token", "ca_bundle"}, "s3")
    if "native" in value: _check_section(value["native"], {"endpoint", "key_id", "application_key", "ca_bundle"}, "native")
    return value


def _check_section(value: Any, allowed: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed: raise ToolError("configuration_invalid", f"{name} configuration is invalid")
    endpoint = value.get("endpoint")
    if endpoint is not None: validate_endpoint(endpoint, native=name == "native")
    for key, reference in value.items():
        if key == "ca_bundle":
            if not isinstance(reference, str) or not reference:
                raise ToolError("configuration_invalid", f"{name}.ca_bundle must name a CA bundle; TLS verification cannot be disabled")
            continue
        if key not in {"access_key_id", "secret_access_key", "session_token", "key_id", "application_key"}: continue
        if not isinstance(reference, dict) or set(reference) not in ({"env"}, {"file"}) or not isinstance(next(iter(reference.values())), str):
            raise ToolError("credential_reference_invalid", f"{name}.{key} must be {{env:NAME}} or {{file:path}}")


def validate_endpoint(endpoint: Any, *, native: bool = False) -> str:
    if not isinstance(endpoint, str): raise ToolError("configuration_invalid", "endpoint must be a URL")
    try:
        parsed = urlparse(endpoint)
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError as exc:
        raise ToolError("unsafe_endpoint", "endpoint URL is malformed") from exc
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.query or parsed.fragment or port not in (None, 443) or parsed.path not in ("", "/"):
        raise ToolError("unsafe_endpoint", "only credential-free HTTPS endpoints are allowed")
    if native and not re.fullmatch(r"api(?:\d+)?\.backblazeb2\.com", host):
        raise ToolError("unsafe_endpoint", "native endpoint must be api.backblazeb2.com or apiNNN.backblazeb2.com")
    if not native and not re.fullmatch(r"s3\.[a-z0-9-]+\.backblazeb2\.com", host):
        raise ToolError("unsafe_endpoint", "S3 endpoint must be s3.<region>.backblazeb2.com")
    return endpoint.rstrip("/")


def resolve(reference: dict[str, str]) -> str:
    if not isinstance(reference, dict) or set(reference) not in ({"env"}, {"file"}) or any(not isinstance(value, str) or not value or "\x00" in value for value in reference.values()):
        raise ToolError("credential_reference_invalid", "secret must use one nonempty environment or protected-file reference")
    if "env" in reference:
        value = os.environ.get(reference["env"])
        if not value: raise ToolError("credential_unavailable", f"credential environment variable {reference['env']} is unset")
        return value
    path = Path(reference["file"])
    try:
        for parent in (path, *path.parents):
            if parent.is_symlink(): raise ToolError("credential_file_unsafe", "credential file path must not contain symlinks")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ToolError("credential_file_unsafe", "credential file must be a regular non-symlink mode 0600 file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(fd)
            if opened.st_ino != info.st_ino or opened.st_dev != info.st_dev or not stat.S_ISREG(opened.st_mode): raise ToolError("credential_file_unsafe", "credential file changed while opening")
            if opened.st_size > 65536:
                raise ToolError("credential_file_unsafe", "credential file exceeds 64 KiB")
            with os.fdopen(os.dup(fd), "rb") as source:
                raw = source.read(65537)
            if len(raw) > 65536:
                raise ToolError("credential_file_unsafe", "credential file exceeds 64 KiB")
            value = raw.decode("utf-8").rstrip("\r\n")
            if not value:
                raise ToolError("credential_unavailable", "credential file is empty")
            return value
        finally: os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise ToolError("credential_unavailable", "credential file cannot be read") from exc


def s3_values(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("s3")
    if not isinstance(section, dict): raise ToolError("configuration_missing", "s3 configuration is required")
    required = ("endpoint", "region", "access_key_id", "secret_access_key")
    if any(key not in section for key in required): raise ToolError("configuration_missing", "s3 endpoint, region, access_key_id, and secret_access_key are required")
    if not isinstance(section["region"], str) or not re.fullmatch(r"[a-z0-9-]+", section["region"]):
        raise ToolError("configuration_invalid", "S3 region must be a nonempty region identifier")
    if urlparse(section["endpoint"]).hostname != "s3." + section["region"] + ".backblazeb2.com":
        raise ToolError("configuration_invalid", "S3 endpoint and signing region must agree")
    budget = config.get("request_budget_seconds", 300)
    if type(budget) is not int or not 1 <= budget <= 300: raise ToolError("configuration_invalid", "request_budget_seconds must be 1..300")
    return {"endpoint_url": validate_endpoint(section["endpoint"]), "region_name": section["region"], "aws_access_key_id": resolve(section["access_key_id"]), "aws_secret_access_key": resolve(section["secret_access_key"]), "aws_session_token": resolve(section["session_token"]) if "session_token" in section else None, "verify": section.get("ca_bundle", True), "request_budget_seconds": budget}


def native_values(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("native")
    if not isinstance(section, dict): raise ToolError("configuration_missing", "native configuration is required")
    if any(key not in section for key in ("endpoint", "key_id", "application_key")): raise ToolError("configuration_missing", "native endpoint, key_id, and application_key are required")
    return {"endpoint": validate_endpoint(section["endpoint"], native=True), "key_id": resolve(section["key_id"]), "application_key": resolve(section["application_key"]), "ca_bundle": section.get("ca_bundle")}
