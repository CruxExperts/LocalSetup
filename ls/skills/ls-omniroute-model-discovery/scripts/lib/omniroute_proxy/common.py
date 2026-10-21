from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass


DEFAULT_BASE_URL = "http://localhost:20128"
MAX_BODY_BYTES = 2_000_000
MAX_TEXT = 500
REDACTED = "***REDACTED***"
SECRET_PATTERNS = (
    (r"sk-[A-Za-z0-9._-]+", REDACTED),
    (r"Bearer\s+[A-Za-z0-9._:-]+", f"Bearer {REDACTED}"),
    (r"(api[_-]?key\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
    (r"(token\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
    (r"(authorization\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
)


@dataclass(frozen=True)
class ProbeTarget:
    name: str
    path: str


TARGETS = [
    ProbeTarget("health", "/api/monitoring/health"),
    ProbeTarget("model_catalog", "/api/models/catalog"),
    ProbeTarget("openai_models", "/v1/models"),
    ProbeTarget("a2a_agent_card", "/.well-known/agent.json"),
    ProbeTarget("rate_limits", "/api/rate-limits"),
    ProbeTarget("resilience", "/api/resilience"),
    ProbeTarget("usage_budget", "/api/usage/budget"),
    ProbeTarget("telemetry_summary", "/api/telemetry/summary"),
]

ACCESS_TARGETS = {
    "runtime": [ProbeTarget("openai_models", "/v1/models")],
    "read": [
        ProbeTarget("health", "/api/monitoring/health"),
        ProbeTarget("model_catalog", "/api/models/catalog"),
    ],
    "write": [
        ProbeTarget("health", "/api/monitoring/health"),
        ProbeTarget("model_catalog", "/api/models/catalog"),
        ProbeTarget("settings_read", "/api/settings"),
    ],
    "admin": [
        ProbeTarget("health", "/api/monitoring/health"),
        ProbeTarget("keys_read", "/api/keys"),
        ProbeTarget("settings_read", "/api/settings"),
    ],
}


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


def sanitize_text(value: object, limit: int = MAX_TEXT) -> str:
    text = redact_text(str(value))
    cleaned = "".join(ch if ch.isprintable() or ch in "\n\t" else "?" for ch in text)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        return cleaned[: limit - 3] + "..."
    return cleaned


def parse_base_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base URL must use http or https")
    if not parsed.netloc:
        raise ValueError("base URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("base URL must not include credentials")
    normalized = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )
    return normalized or DEFAULT_BASE_URL


def join_url(base_url: str, path: str) -> str:
    return f"{base_url}{path}"


def endpoint_hint(url: str) -> str:
    path = urllib.parse.urlparse(url).path or "/"
    if path == "/api/monitoring/health":
        return "Confirm the OmniRoute proxy is running and reachable at --base-url."
    if path.startswith("/v1") or path.startswith("/v1beta") or path == "/api/tags":
        return "Confirm this compatibility route is enabled on the target OmniRoute proxy."
    if path == "/.well-known/agent.json":
        return "A2A discovery may be disabled or unsupported on this proxy version."
    if path.startswith("/api/"):
        return (
            "Confirm the endpoint exists on this OmniRoute version "
            "and check whether bearer auth is required."
        )
    return "Confirm the endpoint path is supported by the target OmniRoute proxy."


def load_api_key(env_name: str | None) -> str | None:
    if not env_name:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("api key environment variable name contains invalid characters")
    value = os.environ.get(env_name)
    if value is None or value.strip() == "":
        return None
    return value.strip()
