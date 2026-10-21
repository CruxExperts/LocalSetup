#!/usr/bin/env python3
"""Deterministic OmniRoute API preflight and request helper."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ACCESS_TARGETS = {
    "runtime": {"openai_models": "/v1/models"},
    "read": {
        "health": "/api/monitoring/health",
        "model_catalog": "/api/models/catalog",
    },
    "write": {
        "health": "/api/monitoring/health",
        "model_catalog": "/api/models/catalog",
        "settings_read": "/api/settings",
    },
    "admin": {
        "health": "/api/monitoring/health",
        "keys_read": "/api/keys",
        "settings_read": "/api/settings",
    },
}

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
REDACTED = "***REDACTED***"
SECRET_PATTERNS = (
    (r"sk-[A-Za-z0-9._-]+", REDACTED),
    (r"Bearer\s+[A-Za-z0-9._:-]+", f"Bearer {REDACTED}"),
    (r"(api[_-]?key\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
    (r"(token\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
    (r"(authorization\s*[=:]\s*)([^\s,;]+)", rf"\1{REDACTED}"),
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic OmniRoute API preflight and request helper"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128"),
        help="OmniRoute base URL; defaults to OMNIROUTE_BASE_URL or localhost",
    )
    parser.add_argument(
        "--api-key-env",
        default="OMNIROUTE_API_KEY",
        help="Environment variable that contains the OmniRoute API key",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output", choices=["json", "text"], default="json")

    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser(
        "preflight", help="Check env presence and non-mutating access compatibility"
    )
    preflight.add_argument(
        "--required-access",
        choices=sorted(ACCESS_TARGETS),
        default="read",
    )
    preflight.add_argument(
        "--fail-on-incompatible",
        action="store_true",
        help="Exit 1 when credentials or requested access are not compatible",
    )

    sub.add_parser("env-commands", help="Print durable user-level env setup commands")

    request = sub.add_parser("request", help="Run one deterministic OmniRoute API call")
    request.add_argument("method", help="HTTP method")
    request.add_argument("path", help="Absolute API path, such as /v1/models")
    request.add_argument(
        "--required-access",
        choices=sorted(ACCESS_TARGETS),
        default="read",
    )
    request.add_argument("--body-json", help="Inline JSON request body")
    request.add_argument("--body-file", help="Path to JSON request body file")
    request.add_argument(
        "--allow-mutation",
        action="store_true",
        help="Required for POST, PUT, PATCH, and DELETE",
    )
    request.add_argument(
        "--fail-on-http-error",
        action="store_true",
        help="Exit 1 when the API returns an HTTP error",
    )

    return parser.parse_args(argv)


def validate_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base-url must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("base-url must not include credentials")
    return base


def validate_env_name(value: str) -> str:
    if not ENV_NAME_RE.fullmatch(value):
        raise ValueError("api key environment variable name is invalid")
    return value


def load_api_key(env_name: str) -> str | None:
    value = os.environ.get(env_name)
    return value.strip() if value and value.strip() else None


def join_url(base_url: str, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("API path must start with /")
    return f"{base_url}{path}"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(token in lowered for token in ("authorization", "api_key", "token", "secret", "password", "cookie")):
                redacted[key] = REDACTED if item else item
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in SECRET_PATTERNS:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    api_key: str | None,
    timeout: float,
    body: Any | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = join_url(base_url, path)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            parsed = parse_jsonish(text)
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "path": path,
                "body": redact(parsed),
            }
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "path": path,
            "error": str(exc),
            "body": redact(parse_jsonish(text)),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "path": path, "error": str(exc)}


def parse_jsonish(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:4000]


def env_registration_commands(base_url: str, api_key_env: str) -> list[str]:
    quoted_base = shlex.quote(base_url)
    quoted_key = shlex.quote("PASTE_OMNIROUTE_API_KEY_HERE")
    return [
        "mkdir -p ~/.config/environment.d",
        "touch ~/.config/environment.d/omniroute.conf",
        (
            "grep -q '^OMNIROUTE_BASE_URL=' ~/.config/environment.d/omniroute.conf || "
            "printf '%s\\n' "
            f"{shlex.quote(f'OMNIROUTE_BASE_URL={base_url}')} "
            ">> ~/.config/environment.d/omniroute.conf"
        ),
        (
            f"grep -q '^{api_key_env}=' ~/.config/environment.d/omniroute.conf || "
            "printf '%s\\n' "
            f"{shlex.quote(f'{api_key_env}=PASTE_OMNIROUTE_API_KEY_HERE')} "
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
            f"{quoted_key} >> ~/.profile"
        ),
        (
            "printf 'Relaunch terminals, tmux sessions, GUI apps, services, and "
            "agent CLIs before expecting them to inherit the new environment.\\n'"
        ),
    ]


def build_preflight_report(
    args: argparse.Namespace, base_url: str, api_key: str | None
) -> dict[str, Any]:
    checks = {
        name: request_json(
            base_url,
            path,
            api_key=api_key,
            timeout=args.timeout,
        )
        for name, path in ACCESS_TARGETS[args.required_access].items()
    }
    failed = [name for name, result in checks.items() if not result.get("ok")]
    report = {
        "ok": bool(api_key) and not failed,
        "required_access": args.required_access,
        "env": {
            "base_url_env_set": bool(os.environ.get("OMNIROUTE_BASE_URL")),
            "api_key_env": args.api_key_env,
            "api_key_env_set": bool(api_key),
            "api_key_value_redacted": "***REDACTED***" if api_key else None,
        },
        "checks": checks,
        "notes": [
            "Preflight uses non-mutating GET endpoints only.",
            "Write/admin compatibility checks do not grant mutation approval.",
            "Relaunch already-running shells, tmux sessions, GUI apps, services, Codex, and OpenCode after registering new env vars.",
        ],
    }
    return report


def command_preflight(args: argparse.Namespace, base_url: str, api_key: str | None) -> int:
    report = build_preflight_report(args, base_url, api_key)
    emit(report, args.output)
    return 1 if args.fail_on_incompatible and not report["ok"] else 0


def load_body(args: argparse.Namespace) -> Any | None:
    if args.body_json and args.body_file:
        raise ValueError("use only one of --body-json or --body-file")
    if args.body_json:
        return json.loads(args.body_json)
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return None


def command_request(args: argparse.Namespace, base_url: str, api_key: str | None) -> int:
    method = args.method.upper()
    if method in MUTATING_METHODS and not args.allow_mutation:
        raise ValueError(f"{method} requires --allow-mutation")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("method must be GET, POST, PUT, PATCH, or DELETE")
    preflight = build_preflight_report(args, base_url, api_key)
    if not preflight["ok"]:
        emit(
            {
                "ok": False,
                "method": method,
                "required_access": args.required_access,
                "preflight": preflight,
                "error": "required access preflight failed; target request was not sent",
            },
            args.output,
        )
        return 1
    body = load_body(args)
    result = request_json(
        base_url,
        args.path,
        method=method,
        api_key=api_key,
        timeout=args.timeout,
        body=body,
    )
    report = {
        "ok": result.get("ok", False),
        "method": method,
        "required_access": args.required_access,
        "result": result,
    }
    emit(report, args.output)
    return 1 if args.fail_on_http_error and not report["ok"] else 0


def emit(payload: dict[str, Any], output: str) -> None:
    if output == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"ok: {payload.get('ok')}")
    for key, value in payload.items():
        if key != "ok":
            print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv or sys.argv[1:])
        base_url = validate_base_url(args.base_url)
        args.api_key_env = validate_env_name(args.api_key_env)
        if args.timeout <= 0 or args.timeout > 120:
            raise ValueError("timeout must be > 0 and <= 120")
        api_key = load_api_key(args.api_key_env)
        if args.command == "preflight":
            return command_preflight(args, base_url, api_key)
        if args.command == "env-commands":
            emit(
                {
                    "ok": True,
                    "api_key_env": args.api_key_env,
                    "commands": env_registration_commands(base_url, args.api_key_env),
                },
                args.output,
            )
            return 0
        if args.command == "request":
            return command_request(args, base_url, api_key)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
