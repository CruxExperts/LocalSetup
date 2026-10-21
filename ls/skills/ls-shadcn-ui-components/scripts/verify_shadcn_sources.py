#!/usr/bin/env python3
"""Validate ls-shadcn-ui-components structure and optionally check upstream source reachability."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType


REQUIRED_FILES = [
    "SKILL.md",
    "references/source-ledger.md",
    "references/cli.md",
    "references/project-context.md",
    "references/components-json.md",
    "references/frameworks.md",
    "references/theming.md",
    "references/forms.md",
    "references/accessibility.md",
    "references/registry.md",
    "references/mcp.md",
    "references/troubleshooting.md",
    "references/update-procedure.md",
    "rules/styling.md",
    "rules/composition.md",
    "rules/forms.md",
    "rules/icons.md",
    "rules/base-vs-radix.md",
    "rules/updates.md",
    "components/index.md",
    "components/forms-inputs.md",
    "components/buttons-actions.md",
    "components/layout-navigation.md",
    "components/overlays-menus.md",
    "components/feedback-status-loading.md",
    "components/data-display-dashboards.md",
    "components/media-typography-utilities.md",
    "components/registry-patterns.md",
    "examples/login-form.md",
    "examples/settings-page.md",
    "examples/dashboard.md",
    "examples/command-dialog.md",
    "examples/destructive-alert-dialog.md",
    "examples/responsive-navigation.md",
    "examples/data-table.md",
    "examples/date-picker-calendar.md",
    "examples/theme-customization.md",
    "examples/monorepo-imports.md",
    "examples/custom-registry.md",
    "examples/safe-component-update.md",
    "tests/fixtures/README.md",
    "tests/validation-checklist.md",
]

NPM_URL = "https://registry.npmjs.org/shadcn"

OFFICIAL_URLS = [
    "https://ui.shadcn.com/docs",
    "https://ui.shadcn.com/docs/cli",
    "https://ui.shadcn.com/docs/changelog",
    "https://ui.shadcn.com/docs/components",
    "https://ui.shadcn.com/docs/components-json",
    "https://ui.shadcn.com/docs/package-imports",
    "https://ui.shadcn.com/docs/theming",
    "https://ui.shadcn.com/docs/forms",
    "https://ui.shadcn.com/docs/registry",
    "https://ui.shadcn.com/docs/mcp",
    "https://ui.shadcn.com/schema.json",
    "https://ui.shadcn.com/schema/registry.json",
    "https://ui.shadcn.com/schema/registry-item.json",
    "https://registry.npmjs.org/shadcn",
]


def load_requests() -> ModuleType:
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "deps.py").is_file():
            sys.path.insert(0, str(parent / "lib"))
            from deps import require_deps

            require_deps(["requests"])
            break

    try:
        return importlib.import_module("requests")
    except ImportError as exc:  # pragma: no cover - environment guidance
        raise SystemExit(
            "Missing dependency: requests. Run `uv sync --locked --no-dev` "
            "from the LocalSetup source checkout."
        ) from exc


def fetch_url(requests_module: ModuleType, url: str, timeout: int) -> dict[str, object]:
    try:
        response = requests_module.get(
            url,
            headers={"User-Agent": "ls-shadcn-ui-verifier/1.0"},
            timeout=timeout,
        )
        data = response.content[: 1024 * 1024]
        return {
            "url": url,
            "ok": 200 <= response.status_code < 400,
            "status": response.status_code,
            "bytes": len(data),
        }
    except requests_module.RequestException as exc:
        return {"url": url, "ok": False, "status": None, "error": str(exc)}


def fetch_json(requests_module: ModuleType, url: str, timeout: int) -> dict[str, object]:
    response = requests_module.get(
        url,
        headers={"User-Agent": "ls-shadcn-ui-verifier/1.0"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def ledger_expected_npm(ledger_text: str) -> tuple[str | None, str | None]:
    version_match = re.search(r"shadcn@([0-9]+\.[0-9]+\.[0-9]+)", ledger_text)
    timestamp_match = re.search(r"`([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z)`", ledger_text)
    version = version_match.group(1) if version_match else None
    timestamp = timestamp_match.group(1) if timestamp_match else None
    return version, timestamp


def validate_static(skill_root: Path) -> list[str]:
    errors: list[str] = []
    for rel_path in REQUIRED_FILES:
        if not (skill_root / rel_path).is_file():
            errors.append(f"missing required file: {rel_path}")

    skill_md = skill_root / "SKILL.md"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8")
        if "name: ls-shadcn-ui-components" not in text:
            errors.append("SKILL.md frontmatter must declare name: ls-shadcn-ui-components")
        if "description:" not in text:
            errors.append("SKILL.md frontmatter must include description")
        if "metadata:" not in text or "version:" not in text:
            errors.append("SKILL.md frontmatter must include metadata.version")

    source_ledger = skill_root / "references/source-ledger.md"
    if source_ledger.is_file():
        ledger = source_ledger.read_text(encoding="utf-8")
        for expected in ["https://ui.shadcn.com/docs", "https://registry.npmjs.org/shadcn"]:
            if expected not in ledger:
                errors.append(f"source ledger missing {expected}")
        if not re.search(r"shadcn@[0-9]+\.[0-9]+\.[0-9]+", ledger):
            errors.append("source ledger missing a dated shadcn package version")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate ls-shadcn-ui-components skill structure and optional source reachability/npm metadata."
    )
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Check official URL reachability and npm latest metadata over the network; does not parse every docs claim.",
    )
    parser.add_argument("--timeout", type=int, default=15, help="Network timeout in seconds for --refresh.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(argv)

    skill_root = args.skill_root.resolve()
    static_errors = validate_static(skill_root)
    refresh_errors: list[str] = []
    refresh_results: list[dict[str, object]] = []
    npm_check: dict[str, object] | None = None

    if args.refresh:
        requests_module = load_requests()
        refresh_results = [
            fetch_url(requests_module, url, args.timeout) for url in OFFICIAL_URLS
        ]
        for item in refresh_results:
            if not item.get("ok"):
                refresh_errors.append(
                    f"source check failed: {item['url']} ({item.get('status') or item.get('error')})"
                )

        source_ledger = skill_root / "references/source-ledger.md"
        if source_ledger.is_file():
            ledger = source_ledger.read_text(encoding="utf-8")
            expected_version, expected_timestamp = ledger_expected_npm(ledger)
            try:
                npm = fetch_json(requests_module, NPM_URL, args.timeout)
                latest = str(npm.get("dist-tags", {}).get("latest", ""))
                latest_time = str(npm.get("time", {}).get(latest, ""))
                npm_check = {
                    "url": NPM_URL,
                    "ok": True,
                    "expected_version": expected_version,
                    "latest_version": latest,
                    "expected_timestamp": expected_timestamp,
                    "latest_timestamp": latest_time,
                }
                if expected_version and latest != expected_version:
                    npm_check["ok"] = False
                    refresh_errors.append(
                        f"npm latest mismatch: ledger {expected_version}, live {latest}"
                    )
                if expected_timestamp and latest_time != expected_timestamp:
                    npm_check["ok"] = False
                    refresh_errors.append(
                        f"npm timestamp mismatch: ledger {expected_timestamp}, live {latest_time}"
                    )
            except (requests_module.RequestException, ValueError) as exc:
                npm_check = {"url": NPM_URL, "ok": False, "error": str(exc)}
                refresh_errors.append(f"npm metadata check failed: {exc}")

    errors = [*static_errors, *refresh_errors]

    payload = {
        "skill_root": str(skill_root),
        "static_ok": not static_errors,
        "static_errors": static_errors,
        "refresh": args.refresh,
        "refresh_ok": not refresh_errors if args.refresh else None,
        "refresh_errors": refresh_errors,
        "refresh_results": refresh_results,
        "npm_check": npm_check,
        "ok": not errors,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"skill_root: {skill_root}")
        print(f"static: {'ok' if payload['static_ok'] else 'failed'}")
        if args.refresh:
            ok_count = sum(1 for item in refresh_results if item.get("ok"))
            print(f"refresh: {ok_count}/{len(refresh_results)} sources ok")
            if npm_check is not None:
                print(f"npm latest: {'ok' if npm_check.get('ok') else 'failed'}")
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
