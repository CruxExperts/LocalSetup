#!/usr/bin/env python3
"""File-backed fake vault used by ls-keepass-secret-references tests and dry demos."""

from __future__ import annotations

import json
import secrets
import string
from pathlib import Path
from typing import Any


SAFE_FIELDS = {"username", "password", "token", "url", "notes", "service_type", "meta"}


class FakeVaultBackend:
    """Small deterministic-ish backend with optional JSON persistence."""

    name = "fake"

    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path
        self.entries: dict[str, dict[str, Any]] = {}
        if store_path and store_path.is_file():
            data = json.loads(store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.entries = {
                    str(key): value for key, value in data.items() if isinstance(value, dict)
                }

    def _save(self) -> None:
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(self.entries, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "entries": len(self.entries),
            "store_path": str(self.store_path) if self.store_path else None,
        }

    def list_entries(self) -> list[dict[str, Any]]:
        return [
            {"id": key, **{k: v for k, v in value.items() if k != "password"}}
            for key, value in sorted(self.entries.items())
        ]

    def search(self, query: str) -> list[dict[str, Any]]:
        q = query.lower()
        return [
            {"id": key, **{k: v for k, v in value.items() if k != "password"}}
            for key, value in sorted(self.entries.items())
            if q in key.lower() or q in str(value.get("username", "")).lower()
        ]

    def resolve(self, secret_id: str, mapping: dict[str, Any] | None = None) -> dict[str, Any]:
        if secret_id not in self.entries:
            base = dict(mapping or {})
            base.setdefault("username", base.get("expected_username") or "")
            base.setdefault("password", f"fake-{secret_id}-password")
            base.setdefault("service_type", base.get("service_type", "service.account"))
            base.setdefault("url", base.get("url", ""))
            base.setdefault("notes", base.get("notes", "fake backend placeholder"))
            self.entries[secret_id] = base
        return {"id": secret_id, **self.entries[secret_id]}

    def ensure(
        self,
        items: list[dict[str, Any]],
        *,
        apply: bool = False,
        rotate: bool = False,
    ) -> dict[str, list[dict[str, str]]]:
        created: list[dict[str, str]] = []
        reused: list[dict[str, str]] = []
        rotated: list[dict[str, str]] = []
        for item in items:
            secret_id = str(item["id"])
            path = str(item.get("path") or secret_id)
            exists = secret_id in self.entries
            if not exists:
                created.append({"id": secret_id, "path": path})
                if apply:
                    self.entries[secret_id] = {
                        "username": item.get("username", ""),
                        "password": item.get("password") or generate_password(),
                        "url": item.get("url", ""),
                        "notes": item.get("notes", ""),
                        "service_type": item.get("service_type", "service.account"),
                        "meta": item.get("meta", {}),
                    }
            elif rotate or item.get("rotate_password"):
                rotated.append({"id": secret_id, "path": path})
                if apply:
                    self.entries[secret_id]["password"] = generate_password()
            else:
                reused.append({"id": secret_id, "path": path})
        if apply:
            self._save()
        return {"created": created, "reused": reused, "rotated": rotated, "errors": []}

    def set_field(self, secret_id: str, field: str, value: str, *, apply: bool = False) -> dict[str, Any]:
        if field not in SAFE_FIELDS:
            raise ValueError(f"unsupported_field: {field}")
        if apply:
            entry = self.entries.setdefault(secret_id, {})
            entry[field] = value
            self._save()
        return {"id": secret_id, "field": field, "changed": bool(apply)}

    def delete(self, secret_id: str, *, apply: bool = False) -> dict[str, Any]:
        existed = secret_id in self.entries
        if apply and existed:
            del self.entries[secret_id]
            self._save()
        return {"id": secret_id, "deleted": bool(apply and existed), "existed": existed}


def generate_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))
