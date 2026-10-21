"""Explicit SSE-C references, request headers, and private key binding."""
from __future__ import annotations

import base64
import hashlib
from typing import Any

from .config import resolve
from .reporting import ToolError


def _key(value: dict[str, Any]) -> bytes:
    reference = value.get("key") or {"env": value.get("key_env")}
    text = resolve(reference)
    # References contain base64-encoded 256-bit keys, never inline key material.
    try:
        key = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ToolError("invalid_encryption_key", "SSE-C reference must contain a base64-encoded 256-bit key") from exc
    if len(key) != 32:
        raise ToolError("invalid_encryption_key", "SSE-C key must contain 32 decoded bytes")
    return key


def parameters(value: Any, *, source: bool = False, customer_only: bool = False) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("algorithm") != "SSE-C":
        raise ToolError("unsupported_encryption", "Garage supports only explicit SSE-C encryption references")
    key = _key(value)
    prefix = "CopySourceSSECustomer" if source else "SSECustomer"
    return {prefix + "Algorithm": "AES256", prefix + "Key": base64.b64encode(key).decode(),
            prefix + "KeyMD5": base64.b64encode(hashlib.md5(key, usedforsecurity=False).digest()).decode()}


def fingerprint(value: Any) -> str | None:
    if value is None:
        return None
    return "SSE-C:" + hashlib.sha256(_key(value)).hexdigest()
