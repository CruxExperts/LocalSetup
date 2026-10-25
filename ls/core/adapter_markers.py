from __future__ import annotations

import json
from pathlib import Path

ADAPTER_MARKER_JSON = ".localsetup-adapter.json"


def adapter_marker_state(repo_path: Path) -> dict:
    marker = repo_path / ADAPTER_MARKER_JSON
    if not marker.exists():
        return {"exists": False, "mode": None, "error": None}
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"exists": True, "mode": None, "error": "adapter marker is not valid JSON"}
    if not isinstance(payload, dict):
        return {"exists": True, "mode": None, "error": "adapter marker is not a JSON object"}
    if "package_bindings" in payload:
        from .package_bindings import adapter_bindings
        try:
            adapter_bindings(repo_path)
        except (OSError, ValueError, KeyError, TypeError):
            return {"exists": True, "mode": None, "error": "adapter package bindings require preservation review"}
    mode = payload.get("mode")
    if mode not in {"symlink", "portable"}:
        return {
            "exists": True,
            "mode": str(mode) if mode is not None else None,
            "error": "adapter marker has unsupported mode",
        }
    return {"exists": True, "mode": str(mode), "error": None}


def is_safe_adapter_package_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    path = Path(name)
    return not path.is_absolute() and len(path.parts) == 1 and path.parts[0] == name


def adapter_marker_packages(repo_path: Path) -> set[str] | None:
    marker = repo_path / ADAPTER_MARKER_JSON
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    packages = payload.get("packages")
    if not isinstance(packages, list):
        return None
    return {str(item) for item in packages if is_safe_adapter_package_name(str(item))}
