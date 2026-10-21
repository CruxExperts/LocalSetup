from __future__ import annotations

import json
import os
import shlex
import time
from typing import Any

import requests

from .common import (
    ACCESS_TARGETS,
    MAX_BODY_BYTES,
    TARGETS,
    endpoint_hint,
    join_url,
    sanitize_text,
)


def summarize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())[:20]
        summary: dict[str, Any] = {"type": "object", "keys": keys}
        for candidate in ("data", "models", "providers", "items"):
            value = payload.get(candidate)
            if isinstance(value, list):
                summary[f"{candidate}_count"] = len(value)
        return summary
    if isinstance(payload, list):
        return {"type": "array", "count": len(payload)}
    if payload is None:
        return {"type": "null"}
    return {"type": type(payload).__name__, "value": sanitize_text(payload)}


def fetch_json(
    session: requests.Session,
    url: str,
    api_key: str | None,
    timeout: float,
    *,
    include_payload: bool = False,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    started = time.monotonic()
    try:
        response = session.get(url, headers=headers, timeout=timeout, stream=True)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                response.close()
                return {
                    "ok": False,
                    "status": status,
                    "elapsed_ms": elapsed_ms,
                    "url": url,
                    "error": f"response exceeded size limit of {MAX_BODY_BYTES} bytes",
                    "hint": (
                        "Use a narrower endpoint or inspect the proxy directly; "
                        "this probe refuses large bodies."
                    ),
                }
            chunks.append(chunk)
        decoded = b"".join(chunks).decode("utf-8", errors="replace")
        try:
            payload = json.loads(decoded) if decoded else None
        except json.JSONDecodeError as exc:
            return {
                "ok": False,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "url": url,
                "content_type": sanitize_text(content_type),
                "error": f"JSONDecodeError: invalid JSON at byte {exc.pos}: {exc.msg}",
                "sample": sanitize_text(decoded),
                "hint": (
                    "Confirm the endpoint returns JSON and that --base-url points "
                    "at the OmniRoute proxy, not a dashboard or reverse-proxy error page."
                ),
            }
        if not response.ok:
            return {
                "ok": False,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "url": url,
                "content_type": sanitize_text(content_type),
                "error": f"HTTP {status}: {sanitize_text(response.reason or 'HTTP error')}",
                "sample": sanitize_text(decoded),
                "hint": endpoint_hint(url),
            }
        result = {
            "ok": True,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "content_type": sanitize_text(content_type),
            "summary": summarize_payload(payload),
        }
        if include_payload:
            result["_payload"] = payload
        return result
    except requests.RequestException as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "status": None,
            "elapsed_ms": elapsed_ms,
            "url": url,
            "error": f"{type(exc).__name__}: {sanitize_text(exc)}",
            "hint": (
                "Check --base-url, local firewall or tunnel settings, DNS, "
                "TLS certificates, and proxy reachability."
            ),
        }


def run_probe(base_url: str, api_key: str | None, timeout: float) -> dict[str, Any]:
    endpoints = {}
    with requests.Session() as session:
        for target in TARGETS:
            endpoints[target.name] = fetch_json(
                session,
                join_url(base_url, target.path),
                api_key,
                timeout,
            )
    return {
        "base_url": base_url,
        "authenticated": bool(api_key),
        "endpoints": endpoints,
    }


def env_registration_commands(base_url: str, api_key_env: str) -> list[str]:
    quoted_base = shlex.quote(base_url)
    quoted_api_placeholder = shlex.quote("PASTE_OMNIROUTE_API_KEY_HERE")
    quoted_base_assignment = shlex.quote(f"OMNIROUTE_BASE_URL={base_url}")
    quoted_key_assignment = shlex.quote(
        f"{api_key_env}=PASTE_OMNIROUTE_API_KEY_HERE"
    )
    return [
        "mkdir -p ~/.config/environment.d",
        "touch ~/.config/environment.d/omniroute.conf",
        (
            "grep -q '^OMNIROUTE_BASE_URL=' ~/.config/environment.d/omniroute.conf || "
            "printf '%s\\n' "
            f"{quoted_base_assignment} "
            ">> ~/.config/environment.d/omniroute.conf"
        ),
        (
            f"grep -q '^{api_key_env}=' ~/.config/environment.d/omniroute.conf || "
            "printf '%s\\n' "
            f"{quoted_key_assignment} "
            ">> ~/.config/environment.d/omniroute.conf"
        ),
        "touch ~/.profile",
        (
            "grep -q '^export OMNIROUTE_BASE_URL=' ~/.profile || "
            "printf '\\nexport OMNIROUTE_BASE_URL=\"${OMNIROUTE_BASE_URL:-%s}\"\\n' "
            f"{quoted_base} >> ~/.profile"
        ),
        (
            f"grep -q '^export {api_key_env}=' ~/.profile || "
            f"printf 'export {api_key_env}=\"${{{api_key_env}:-%s}}\"\\n' "
            f"{quoted_api_placeholder} >> ~/.profile"
        ),
        (
            "printf 'Relaunch terminals, tmux sessions, GUI apps, and Codex/OpenCode "
            "processes before expecting them to inherit the new environment.\\n'"
        ),
    ]


def access_preflight(
    base_url: str,
    api_key_env: str,
    api_key: str | None,
    required_access: str,
    timeout: float,
    include_env_commands: bool,
    fetcher: Any = None,
) -> dict[str, Any]:
    fetcher = fetcher or fetch_json
    checks = {}
    with requests.Session() as session:
        for target in ACCESS_TARGETS[required_access]:
            checks[target.name] = fetcher(
                session,
                join_url(base_url, target.path),
                api_key,
                timeout,
            )
    failed = {
        name: result
        for name, result in checks.items()
        if not isinstance(result, dict) or not result.get("ok")
    }
    env = {
        "base_url_env_set": bool(os.environ.get("OMNIROUTE_BASE_URL")),
        "api_key_env": api_key_env,
        "api_key_env_set": bool(api_key),
        "api_key_value_redacted": "***REDACTED***" if api_key else None,
    }
    report: dict[str, Any] = {
        "base_url": base_url,
        "required_access": required_access,
        "env": env,
        "access_ok": bool(api_key) and not failed,
        "access_checks": checks,
        "notes": [
            "Preflight uses non-mutating GET endpoints only.",
            "A write requirement confirms admin-compatible read access only.",
            "Running shells and clients may require relaunch after env changes.",
        ],
    }
    if include_env_commands:
        report["registration_commands"] = env_registration_commands(
            base_url,
            api_key_env,
        )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OmniRoute discovery report",
        "",
        f"Base URL: `{report['base_url']}`",
        f"Authenticated: `{'yes' if report['authenticated'] else 'no'}`",
        "",
        "| Endpoint | OK | Status | Time | Summary |",
        "|---|---:|---:|---:|---|",
    ]
    endpoints = report.get("endpoints", {})
    if not isinstance(endpoints, dict):
        return "# OmniRoute discovery report\n\nInvalid report shape."
    for name, result in endpoints.items():
        if not isinstance(result, dict):
            continue
        status = result.get("status") if result.get("status") is not None else "n/a"
        summary = result.get("summary") if result.get("ok") else result.get("error", "failed")
        if not result.get("ok") and result.get("hint"):
            summary = f"{summary}; hint: {result['hint']}"
        lines.append(
            f"| `{sanitize_text(name)}` | {'yes' if result.get('ok') else 'no'} | "
            f"{status} | {result.get('elapsed_ms', 'n/a')} ms | "
            f"{sanitize_text(summary)} |"
        )
    lines.extend(
        [
            "",
            "Notes:",
            "- This script only uses read-only endpoints.",
            "- Missing endpoints may reflect version, feature, or auth differences.",
            "- Treat capability and limit fields as partial unless reported.",
        ]
    )
    return "\n".join(lines)


def render_preflight_markdown(report: dict[str, Any]) -> str:
    env = report.get("env", {})
    lines = [
        "# OmniRoute preflight report",
        "",
        f"Base URL: `{report.get('base_url')}`",
        f"Required access: `{report.get('required_access')}`",
        f"API key env var: `{env.get('api_key_env')}`",
        f"API key present: `{'yes' if env.get('api_key_env_set') else 'no'}`",
        f"Access compatible: `{'yes' if report.get('access_ok') else 'no'}`",
        "",
        "| Check | OK | Status | Detail |",
        "|---|---:|---:|---|",
    ]
    checks = report.get("access_checks", {})
    if isinstance(checks, dict):
        for name, result in checks.items():
            if not isinstance(result, dict):
                continue
            detail = result.get("summary") if result.get("ok") else result.get("error")
            if not result.get("ok") and result.get("hint"):
                detail = f"{detail}; hint: {result['hint']}"
            status = result.get("status") if result.get("status") is not None else "n/a"
            lines.append(
                f"| `{sanitize_text(name)}` | {'yes' if result.get('ok') else 'no'} | "
                f"{status} | {sanitize_text(detail)} |"
            )
    commands = report.get("registration_commands")
    if isinstance(commands, list) and commands:
        lines.extend(
            [
                "",
                "## Persistent user env registration",
                "",
                "Replace `PASTE_OMNIROUTE_API_KEY_HERE` with the real key:",
                "",
                "```bash",
                *(str(command) for command in commands),
                "```",
            ]
        )
    notes = report.get("notes")
    if isinstance(notes, list) and notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"- {sanitize_text(note)}" for note in notes)
    return "\n".join(lines)
