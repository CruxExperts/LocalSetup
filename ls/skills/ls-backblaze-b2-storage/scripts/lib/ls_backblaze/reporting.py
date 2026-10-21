"""Stable JSON envelopes and redaction for the Backblaze CLI."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from . import SCHEMA_VERSION

EXIT = {"ok": 0, "input": 2, "policy": 3, "service": 4, "uncertain": 5, "interrupt": 130}
SENSITIVE = {"authorizationtoken", "applicationkey", "secret", "secretaccesskey", "sessiontoken", "password", "hmacsha256signingsecret"}


def scrub(value: Any, key: str = "") -> Any:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    if normalized in SENSITIVE or normalized == "customheaders":
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def envelope(operation: str, *, outcome: str, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "operation": operation, "ok": outcome in {"planned", "succeeded"}, "outcome": outcome}
    if error is not None:
        data["error"] = scrub(error)
    else:
        data["result"] = scrub(result if result is not None else {})
    return data


class ToolError(Exception):
    def __init__(self, code: str, message: str, exit_name: str = "input", retryable: bool = False, reconciliation: str | None = None):
        super().__init__(message)
        self.code, self.message, self.exit_name = code, message, exit_name
        self.retryable, self.reconciliation = retryable, reconciliation

    def as_envelope(self, operation: str) -> dict[str, Any]:
        outcome = "unknown" if self.exit_name == "uncertain" else "failed"
        error: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if hasattr(self, "members"):
            error["members"] = self.members
        if self.reconciliation:
            error["reconciliation"] = self.reconciliation
        return envelope(operation, outcome=outcome, error=error)


def emit(data: dict[str, Any]) -> None:
    print(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False))
