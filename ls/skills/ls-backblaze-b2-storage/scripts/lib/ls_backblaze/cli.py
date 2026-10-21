"""Thin command-line boundary for a single Backblaze JSON operation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .config import load_config, native_values, s3_values
from .native import execute as native_execute
from .reporting import EXIT, ToolError, emit, envelope
from .s3 import execute as s3_execute
from .validation import ACCESS, DESTRUCTIVE, WRITES, operation_parts, request_schema, result_schema, validate


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ToolError("command_line_invalid", message)


def parser() -> argparse.ArgumentParser:
    result = JsonParser(description="Backblaze B2 S3 and native JSON operation CLI")
    result.add_argument("--tool", help="s3.<Operation> or native.<Operation>")
    group = result.add_mutually_exclusive_group()
    group.add_argument("--args-json", help="JSON request argument object")
    group.add_argument("--args-file", help="JSON request argument file; - reads stdin")
    result.add_argument("--config", help="JSON file containing explicit protected credential references")
    result.add_argument("--apply", action="store_true", help="perform the explicitly requested operation")
    result.add_argument("--allow-destructive", action="store_true")
    result.add_argument("--allow-access-change", action="store_true")
    result.add_argument("--allow-public", action="store_true")
    result.add_argument("--allow-overwrite", action="store_true")
    result.add_argument("--schema", choices=("request", "result"), help="emit offline JSON Schema")
    result.add_argument("--capabilities", action="store_true", help="emit offline supported-operation metadata")
    return result


def _args(parsed: argparse.Namespace) -> dict[str, Any]:
    if parsed.args_json is None and parsed.args_file is None: raise ToolError("arguments_missing", "one of --args-json or --args-file is required")
    raw = parsed.args_json
    if parsed.args_file is not None:
        try:
            if parsed.args_file == "-": raw = sys.stdin.read(1024 * 1024 + 1)
            else:
                argument_path = Path(parsed.args_file)
                if argument_path.stat().st_size > 1024 * 1024: raise ToolError("arguments_too_large", "argument JSON is limited to 1 MiB")
                raw = argument_path.read_text(encoding="utf-8")
        except OSError as exc: raise ToolError("arguments_unreadable", "could not read argument file") from exc
    if raw is not None and len(raw.encode("utf-8")) > 1024 * 1024: raise ToolError("arguments_too_large", "argument JSON is limited to 1 MiB")
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result: raise ToolError("duplicate_field", f"duplicate JSON field: {key}")
            result[key] = value
        return result
    try: value = json.loads(raw, object_pairs_hook=unique_pairs)
    except (TypeError, json.JSONDecodeError) as exc: raise ToolError("arguments_invalid", "arguments must be valid JSON") from exc
    return value


def _safety(operation: str, namespace: str, name: str, args: dict[str, Any], parsed: argparse.Namespace) -> None:
    if operation in DESTRUCTIVE and not parsed.allow_destructive: raise ToolError("destructive_not_allowed", "operation requires --allow-destructive", "policy")
    if (args.get("bypass_governance") or (name == "PutObjectLegalHold" and args.get("status") == "OFF")) and not parsed.allow_destructive:
        raise ToolError("destructive_not_allowed", "releasing a legal hold or bypassing governance requires --allow-destructive", "policy")
    if (name in ACCESS or args.get("object_lock_enabled")) and not parsed.allow_access_change: raise ToolError("access_change_not_allowed", "operation requires --allow-access-change", "policy")
    if args.get("acl") == "public-read" and not parsed.allow_public: raise ToolError("public_not_allowed", "public-read ACL requires --allow-public", "policy")
    if namespace == "native" and name in {"CreateBucket", "UpdateBucket"} and args.get("bucket_type") == "allPublic" and not parsed.allow_public: raise ToolError("public_not_allowed", "allPublic bucket requires --allow-public", "policy")
    if namespace == "native" and name in {"CreateBucket", "UpdateBucket", "CreateKey"} and not parsed.allow_access_change: raise ToolError("access_change_not_allowed", "native access-changing operation requires --allow-access-change", "policy")
    if name in {"PutObject", "CopyObject", "CompleteMultipartUpload", "UploadFileMultipart", "ResumeMultipartUpload"} and not parsed.allow_overwrite: raise ToolError("overwrite_not_allowed", "object replacement requires --allow-overwrite", "policy")


def capabilities() -> dict[str, Any]:
    from .validation import S3, NATIVE
    return {"schema_version": SCHEMA_VERSION, "provider": "backblaze-b2", "offline_plan_default": True, "operations": {"s3": sorted(S3), "native": sorted(NATIVE)}, "unsupported": ["POST presign", "S3 lifecycle/notification APIs", "S3 tag writes", "KMS", "S3 policies", "website", "PutBucketVersioning"]}


def main(argv: list[str] | None = None) -> int:
    operation, mutation_started = "", False
    try:
        parsed = parser().parse_args(argv)
        operation = parsed.tool or ""
        if parsed.schema:
            emit(request_schema() if parsed.schema == "request" else result_schema()); return 0
        if parsed.capabilities:
            emit(capabilities()); return 0
        args = _args(parsed); namespace, name, args = validate(operation, args); _safety(operation, namespace, name, args, parsed)
        config = load_config(parsed.config) if parsed.config else None
        if not parsed.apply:
            emit(envelope(operation, outcome="planned", result={"network": False, "validated": True, "apply_required": True})); return 0
        if config is None:
            raise ToolError("configuration_missing", "--apply requires --config")
        mutation_started = name in WRITES or name == "ResumeMultipartUpload"
        if namespace == "s3": result = s3_execute(name, args, s3_values(config))
        else: result = native_execute(name, args, native_values(config), config.get("request_budget_seconds", 300))
        if isinstance(result, dict) and result.get("partial"):
            emit(envelope(operation, outcome="partial", result=result)); return EXIT["uncertain"]
        emit(envelope(operation, outcome="succeeded", result=result)); return 0
    except KeyboardInterrupt:
        emit(envelope(operation, outcome="unknown" if mutation_started else "failed", error={"code": "interrupted", "message": "operation interrupted", "retryable": False, "reconciliation": "inspect the corresponding read-only get/list result and any transfer checkpoint before retrying"})); return EXIT["interrupt"]
    except ToolError as exc:
        emit(exc.as_envelope(operation)); return EXIT[exc.exit_name]
    except Exception:
        emit(envelope(operation, outcome="unknown" if mutation_started else "failed", error={"code": "unexpected_failure", "message": "operation failed unexpectedly", "retryable": False, "reconciliation": "inspect the corresponding read-only get/list result and any transfer checkpoint before retrying"})); return EXIT["uncertain"] if mutation_started else EXIT["service"]


if __name__ == "__main__": raise SystemExit(main())
