"""The offline-first command boundary for one Garage JSON operation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, admin_schema
from .admin import execute as admin_execute
from .config import admin_values, load_config, s3_values
from .reporting import EXIT, ToolError, emit, envelope
from .result_shapes import schema as result_schema
from .s3 import execute as s3_execute
from .storage_shapes import S3
from .validation import ACCESS, DESTRUCTIVE, OVERWRITE, WRITES, request_schema, validate


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ToolError("command_line_invalid", message)


def parser() -> argparse.ArgumentParser:
    value = Parser(description="Garage Admin v2 and documented S3 JSON operation CLI")
    value.add_argument("--tool")
    group = value.add_mutually_exclusive_group()
    group.add_argument("--args-json")
    group.add_argument("--args-file", help="JSON file; - reads standard input")
    value.add_argument("--config")
    value.add_argument("--apply", action="store_true")
    value.add_argument("--allow-destructive", action="store_true")
    value.add_argument("--allow-access-change", action="store_true")
    value.add_argument("--allow-public", action="store_true")
    value.add_argument("--allow-overwrite", action="store_true")
    value.add_argument("--schema", choices=("request", "result"))
    value.add_argument("--capabilities", action="store_true")
    return value


def _arguments(parsed: argparse.Namespace) -> dict[str, Any]:
    if parsed.args_json is None and parsed.args_file is None:
        raise ToolError("arguments_missing", "one of --args-json or --args-file is required")
    try:
        raw = parsed.args_json
        if parsed.args_file is not None:
            if parsed.args_file == "-": raw = sys.stdin.read(1024 * 1024 + 1)
            else:
                path = Path(parsed.args_file)
                if path.stat().st_size > 1024 * 1024: raise ToolError("arguments_too_large", "argument JSON is limited to1MiB")
                raw = path.read_text(encoding="utf-8")
        if raw is None or len(raw.encode("utf-8")) > 1024 * 1024: raise ToolError("arguments_too_large", "argument JSON is limited to1MiB")
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            output: dict[str, Any] = {}
            for key, item in pairs:
                if key in output: raise ToolError("duplicate_field", f"duplicate JSON field: {key}")
                output[key] = item
            return output
        value = json.loads(raw, object_pairs_hook=unique)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolError("arguments_invalid", "arguments must be valid JSON") from exc
    if not isinstance(value, dict): raise ToolError("invalid_argument", "arguments must be a JSON object")
    return value


def _safety(operation: str, name: str, values: dict[str, Any], parsed: argparse.Namespace) -> None:
    if operation in DESTRUCTIVE and not parsed.allow_destructive:
        raise ToolError("destructive_not_allowed", "operation requires --allow-destructive", "policy")
    if name in ACCESS and not parsed.allow_access_change:
        raise ToolError("access_change_not_allowed", "operation requires --allow-access-change", "policy")
    if name in OVERWRITE and not parsed.allow_overwrite:
        raise ToolError("overwrite_not_allowed", "operation requires --allow-overwrite", "policy")
    if name in {"PutBucketWebsite"} or (operation == "admin.UpdateBucket" and values.get("websiteAccess", {}).get("enabled") is True):
        if not parsed.allow_public: raise ToolError("public_not_allowed", "enabling a website requires --allow-public", "policy")


def capabilities() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "provider": "garage-v2.3.0", "offline_plan_default": True,
            "operations": {"s3": sorted(S3), "admin": sorted(admin_schema.OPERATIONS)},
            "unsupported": ["versions and version recovery", "conditional no-clobber", "S3 ACLs/policies", "default encryption", "Object Lock", "retention", "legal holds", "replication", "notifications"]}


def main(argv: list[str] | None = None) -> int:
    operation, mutation_started = "", False
    try:
        parsed = parser().parse_args(argv)
        if parsed.schema:
            emit(request_schema() if parsed.schema == "request" else result_schema()); return 0
        if parsed.capabilities:
            emit(capabilities()); return 0
        operation = parsed.tool or ""
        values = _arguments(parsed)
        namespace, name, values = validate(operation, values)
        _safety(operation, name, values, parsed)
        config = load_config(parsed.config) if parsed.config else None
        if not parsed.apply:
            emit(envelope(operation, outcome="planned", result={"network": False, "validated": True, "apply_required": True})); return 0
        if config is None: raise ToolError("configuration_missing", "--apply requires --config")
        mutation_started = name in WRITES
        result = admin_execute(name, values, admin_values(config)) if namespace == "admin" else s3_execute(name, values, s3_values(config))
        if isinstance(result, dict) and result.get("partial"):
            emit(envelope(operation, outcome="partial", result=result)); return EXIT["uncertain"]
        emit(envelope(operation, outcome="succeeded", result=result)); return 0
    except KeyboardInterrupt:
        emit(envelope(operation, outcome="unknown" if mutation_started else "failed", error={"code": "interrupted", "message": "operation interrupted", "retryable": False, "reconciliation": "inspect the corresponding read-only operation before retrying"})); return EXIT["interrupt"]
    except ToolError as exc:
        emit(exc.as_envelope(operation)); return EXIT[exc.exit_name]
    except Exception:
        emit(envelope(operation, outcome="unknown" if mutation_started else "failed", error={"code": "unexpected_failure", "message": "operation failed unexpectedly", "retryable": False, "reconciliation": "inspect the corresponding read-only operation before retrying"})); return EXIT["uncertain"] if mutation_started else EXIT["service"]
