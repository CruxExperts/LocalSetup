from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime

from .common import DEFAULT_BASE_URL, load_api_key, parse_base_url, sanitize_text
from .observation import run_model_observation
from .observation_contract import ObservationError
from .probe import (
    access_preflight,
    render_markdown,
    render_preflight_markdown,
    run_probe,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only OmniRoute discovery probe")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OMNIROUTE_BASE_URL", DEFAULT_BASE_URL),
        help="OmniRoute base URL, default: OMNIROUTE_BASE_URL or http://localhost:20128",
    )
    parser.add_argument(
        "--api-key-env",
        default="OMNIROUTE_API_KEY",
        help="Environment variable containing bearer token; use empty string to disable",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-endpoint timeout in seconds, default: 5",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check required OmniRoute env vars and non-mutating access compatibility",
    )
    parser.add_argument(
        "--model-observation",
        action="store_true",
        help="Emit a sanitized model observation from /api/models/catalog and /v1/models only",
    )
    parser.add_argument(
        "--required-access",
        choices=["runtime", "read", "write", "admin"],
        default="runtime",
        help="Access level to check when --preflight is used",
    )
    parser.add_argument(
        "--print-env-commands",
        action="store_true",
        help="Include durable user-level env registration commands in preflight output",
    )
    parser.add_argument(
        "--fail-on-incompatible",
        action="store_true",
        help="Return exit code 1 when --preflight access is not compatible",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit JSON report")
    output.add_argument("--markdown", action="store_true", help="Emit Markdown report")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.timeout <= 0 or args.timeout > 60:
            raise ValueError("timeout must be greater than 0 and no more than 60 seconds")
        base_url = parse_base_url(args.base_url)
        api_key_env = args.api_key_env.strip() or None
        api_key = load_api_key(api_key_env)
        if args.preflight and args.model_observation:
            raise ValueError("--preflight and --model-observation are mutually exclusive")
        if args.model_observation and args.markdown:
            raise ValueError("--model-observation supports JSON output only")
        if args.preflight:
            if not api_key_env:
                raise ValueError("--preflight requires a non-empty --api-key-env")
            report = access_preflight(
                base_url,
                api_key_env,
                api_key,
                args.required_access,
                args.timeout,
                args.print_env_commands,
            )
        elif args.model_observation:
            report = run_model_observation(
                base_url,
                api_key,
                args.timeout,
                observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        else:
            report = run_probe(base_url, api_key, args.timeout)
    except (ValueError, ObservationError) as exc:
        reason = str(exc) if isinstance(exc, ObservationError) else sanitize_text(exc)
        print(f"omniroute_discover.py: invalid input: {reason}", file=os.sys.stderr)
        return 2

    if args.markdown:
        print(render_preflight_markdown(report) if args.preflight else render_markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    if args.preflight and args.fail_on_incompatible and not report.get("access_ok"):
        return 1
    return 0
