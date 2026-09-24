"""Authorized file-drop shipping of one opaque signed Agent Q envelope."""
from __future__ import annotations
import json
import os
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any
from .crypto_pipeline import CryptoPipelineError, seal_manifest
from .registry import load_registry_yaml, require_file_drop_path

SUFFIX = ".agentq.lspgp"

def _write_blob(blob: bytes, out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(out_dir).free < len(blob) + 65536:
        raise CryptoPipelineError("DISK_FULL")
    opaque = secrets.token_hex(20)
    sealed = out_dir / (opaque + SUFFIX)
    ready = out_dir / (opaque + ".ready")
    with tempfile.NamedTemporaryFile(mode="wb", dir=out_dir, prefix=".tmp_", delete=False) as target:
        temp = Path(target.name)
        target.write(blob)
        target.flush()
        os.fsync(target.fileno())
    try:
        temp.replace(sealed)
        with ready.open("x", encoding="utf-8") as marker:
            marker.write("")
            marker.flush()
            os.fsync(marker.fileno())
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {"status": "ok", "sealed": str(sealed), "ready": str(ready)}

def ship_file_drop(manifest: dict[str, Any], registry_path: Path, out_dir: Path, stem: str = "", *,
                   queue_root: Path | None = None, skip_pre_ship: bool = False,
                   pre_ship_cwd: Path | None = None) -> dict[str, Any]:
    from .ledger import append_ship_event
    from .preship import run_pre_ship_checks
    try:
        registry = load_registry_yaml(registry_path)
        ids = manifest.get("to_agent_ids")
        if not isinstance(ids, list) or not ids:
            raise CryptoPipelineError("RECIPIENT_SET_INVALID")
        for peer_id in ids:
            require_file_drop_path(registry, peer_id, out_dir, inbound=False)
        if not skip_pre_ship:
            check = run_pre_ship_checks(manifest, cwd=pre_ship_cwd)
            if not check.get("ok"):
                if queue_root:
                    append_ship_event(queue_root, "ship_push_fail", {"code": "PRE_SHIP_FAILED"})
                return {"status": "error", "code": "PRE_SHIP_FAILED", "detail": check}
        blob = seal_manifest(manifest, registry)
        result = _write_blob(blob, out_dir)
    except Exception as exc:
        result = {"status": "error", "code": str(getattr(exc, "code", "SHIP_FAILED"))}
    if queue_root:
        append_ship_event(queue_root, "ship_push_ok" if result["status"] == "ok" else "ship_push_fail",
                          {"code": result.get("code"), "sealed": result.get("sealed")})
    return result

def ship_file_drop_multi(manifest: dict[str, Any], registry_path: Path, out_dir: Path,
                         stem: str = "", **kwargs: Any) -> list[dict[str, Any]]:
    """Seal once for the exact set; one file-drop object can be shared by all recipients."""
    return [ship_file_drop(manifest, registry_path, out_dir, stem, **kwargs)]

def load_manifest_from_path(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("manifest must be JSON with explicit from_agent_id and to_agent_ids")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("manifest too large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be object")
    return manifest
