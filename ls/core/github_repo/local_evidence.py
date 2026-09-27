"""Bounded local repository-content evidence and social-preview file binding."""

from __future__ import annotations

import hashlib
import errno
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from typing import Any

from .checkout import _CheckoutFailure, _root_binding, _run_git


_MAX_TRACKED_FILE_BYTES = 1_000_000
_MAX_TOTAL_TRACKED_BYTES = 8_000_000
_MAX_SOCIAL_PREVIEW_BYTES = 1_000_000
_GIT_QUERY_TIMEOUT_SECONDS = 8.0
_SOCIAL_PREVIEW_FORMATS = {
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "gif": (b"GIF87a", b"GIF89a"),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BADGE_LINK = re.compile(rb"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")

_CONTROL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "readme_presentation": ("README.md", "README.rst", "README.txt"),
    "status_badges": ("README.md", "README.rst", "README.txt"),
    "installation_route": (
        "INSTALL.md", "INSTALL",
        "docs/INSTALL.md", "docs/installation.md", "ls/docs/MULTI_PLATFORM_INSTALL.md", "install",
    ),
    "support_route": ("SUPPORT.md", ".github/SUPPORT.md", "docs/SUPPORT.md", "ls/docs/SUPPORT.md"),
    "contribution_route": ("CONTRIBUTING.md", ".github/CONTRIBUTING.md"),
    "security_reporting_route": ("SECURITY.md", ".github/SECURITY.md"),
    "changelog_version_signals": (
        "CHANGELOG.md", "CHANGES.md", "HISTORY.md", "NEWS.md", "VERSION", "pyproject.toml",
    ),
    "dependabot_configuration": (".github/dependabot.yml", ".github/dependabot.yaml"),
    "community_files": (
        "LICENSE", "LICENSE.md", "COPYING", "NOTICE", "CODE_OF_CONDUCT.md",
        ".github/CODE_OF_CONDUCT.md", ".github/ISSUE_TEMPLATE.md", ".github/ISSUE_TEMPLATE/config.yml",
        ".github/PULL_REQUEST_TEMPLATE.md", ".github/FUNDING.yml",
    ),
    "pages_site_metadata": (
        "CNAME", "_config.yml", "mkdocs.yml", "docusaurus.config.js", "docusaurus.config.ts",
        "index.html", "docs/index.html",
    ),
    "open_graph_metadata_inputs": (
        "index.html", "docs/index.html", "_config.yml", "mkdocs.yml", "docusaurus.config.js", "docusaurus.config.ts",
    ),
    "accessibility_inputs": (
        "ACCESSIBILITY.md", "A11Y.md", "index.html", "docs/index.html", "README.md", "README.rst", "README.txt",
    ),
    "footer_attribution_inputs": (
        "README.md", "index.html", "docs/index.html", "NOTICE", "COPYRIGHT", "COPYRIGHT.md", "LICENSE", "LICENSE.md",
    ),
}
_ALL_CANDIDATES = tuple(sorted({path for paths in _CONTROL_CANDIDATES.values() for path in paths}))


class _EvidenceFailure(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _checkout_root(value: str | os.PathLike[str]) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise _EvidenceFailure("checkout_missing") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _EvidenceFailure("invalid_checkout_path") from exc
    if not root.is_dir():
        raise _EvidenceFailure("checkout_not_directory")

    deadline = time.monotonic() + _GIT_QUERY_TIMEOUT_SECONDS
    try:
        output, code = _run_git(root, ["rev-parse", "--show-toplevel"], deadline)
    except _CheckoutFailure as exc:
        raise _EvidenceFailure(exc.reason) from exc
    if code != 0:
        raise _EvidenceFailure("not_git_repository")
    if not output.endswith(b"\n") or output.count(b"\n") != 1:
        raise _EvidenceFailure("invalid_git_state")
    try:
        git_root = Path(os.fsdecode(output[:-1])).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _EvidenceFailure("invalid_git_state") from exc
    if not git_root.is_dir() or git_root != root:
        raise _EvidenceFailure("checkout_root_mismatch")
    return root


def _tracked_candidates(root: Path) -> set[str]:
    pathspecs = [f":(literal){item}" for item in _ALL_CANDIDATES]
    try:
        output, returncode = _run_git(
            root,
            ["ls-files", "-z", "--", *pathspecs],
            time.monotonic() + _GIT_QUERY_TIMEOUT_SECONDS,
        )
    except _CheckoutFailure as exc:
        raise _EvidenceFailure(exc.reason) from exc
    if returncode != 0:
        raise _EvidenceFailure("git_index_unavailable")
    if not isinstance(output, bytes) or (output and not output.endswith(b"\0")):
        raise _EvidenceFailure("invalid_git_state")
    try:
        found = {item.decode("utf-8", errors="strict") for item in output.split(b"\0") if item}
    except UnicodeDecodeError as exc:
        raise _EvidenceFailure("invalid_git_state") from exc
    if not found <= set(_ALL_CANDIDATES):
        raise _EvidenceFailure("invalid_git_state")
    return found


def _relative_parts(value: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise _EvidenceFailure("asset_path_invalid")
    path = PurePosixPath(value)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise _EvidenceFailure("asset_path_invalid")
    normalized = "/".join(parts)
    if normalized != value:
        raise _EvidenceFailure("asset_path_invalid")
    return normalized, parts


def _open_beneath(root: Path, parts: tuple[str, ...]) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise _EvidenceFailure("safe_file_open_unavailable")
    common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        current_fd = os.open(root, common_flags | directory_flag)
    except OSError as exc:
        raise _EvidenceFailure("file_unavailable") from exc
    try:
        for part in parts[:-1]:
            try:
                directory_info = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    raise _EvidenceFailure("file_missing") from exc
                raise _EvidenceFailure("file_unavailable") from exc
            if stat.S_ISLNK(directory_info.st_mode):
                raise _EvidenceFailure("symlink_not_allowed")
            if not stat.S_ISDIR(directory_info.st_mode):
                raise _EvidenceFailure("path_component_not_directory")
            next_fd = os.open(part, common_flags | directory_flag, dir_fd=current_fd)
            try:
                opened_directory = os.fstat(next_fd)
            except OSError:
                os.close(next_fd)
                raise
            if (opened_directory.st_dev, opened_directory.st_ino) != (directory_info.st_dev, directory_info.st_ino):
                os.close(next_fd)
                raise _EvidenceFailure("file_changed_during_read")
            os.close(current_fd)
            current_fd = next_fd
        try:
            entry_info = os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise _EvidenceFailure("file_missing") from exc
            raise _EvidenceFailure("file_unavailable") from exc
        if stat.S_ISLNK(entry_info.st_mode):
            raise _EvidenceFailure("symlink_not_allowed")
        if not stat.S_ISREG(entry_info.st_mode):
            raise _EvidenceFailure("regular_file_required")
        file_fd: int | None = None
        try:
            file_fd = os.open(parts[-1], common_flags | getattr(os, "O_NONBLOCK", 0), dir_fd=current_fd)
            opened_info = os.fstat(file_fd)
        except OSError:
            if file_fd is not None:
                os.close(file_fd)
            raise
        assert file_fd is not None
        if (opened_info.st_dev, opened_info.st_ino) != (entry_info.st_dev, entry_info.st_ino):
            os.close(file_fd)
            raise _EvidenceFailure("file_changed_during_read")
        return file_fd
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _EvidenceFailure("symlink_not_allowed") from exc
        if exc.errno == errno.ENOTDIR:
            raise _EvidenceFailure("path_component_not_directory") from exc
        if exc.errno == errno.ENOENT:
            raise _EvidenceFailure("file_missing") from exc
        raise _EvidenceFailure("file_unavailable") from exc
    finally:
        os.close(current_fd)


def _read_regular_file(root: Path, relative_path: str, limit: int) -> tuple[bytes, os.stat_result]:
    _, parts = _relative_parts(relative_path)
    try:
        descriptor = _open_beneath(root, parts)
    except _EvidenceFailure:
        raise
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _EvidenceFailure("regular_file_required")
        if before.st_size < 0 or before.st_size > limit:
            raise _EvidenceFailure("file_size_limit_exceeded")
        content = bytearray()
        while len(content) <= limit:
            try:
                chunk = os.read(descriptor, min(65536, limit + 1 - len(content)))
            except OSError as exc:
                raise _EvidenceFailure("file_unavailable") from exc
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > limit:
                raise _EvidenceFailure("file_size_limit_exceeded")
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise _EvidenceFailure("file_unavailable") from exc
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        ) or len(content) != after.st_size:
            raise _EvidenceFailure("file_changed_during_read")
        return bytes(content), after
    finally:
        os.close(descriptor)


def _control_record(
    control: str,
    candidate_paths: tuple[str, ...],
    tracked_paths: set[str],
    file_hashes: dict[str, dict[str, Any]],
    read_failures: dict[str, str],
    badge_counts: dict[str, int],
) -> dict[str, Any]:
    present = sorted(set(candidate_paths) & tracked_paths)
    available = [path for path in present if path in file_hashes]
    failures = {path: read_failures[path] for path in present if path in read_failures}
    if failures and available:
        status_value = "partially_observed"
    elif failures:
        status_value = "unavailable"
    else:
        status_value = "observed"
    value: dict[str, Any] = {"present": bool(present), "tracked_paths": present}
    if control == "status_badges":
        value["markdown_badge_reference_count"] = (
            sum(badge_counts[path] for path in available) if present and available else (0 if not present else None)
        )
    return {
        "control": control,
        "status": status_value,
        "applicability": "applicable",
        "authority": "local",
        "capability": "structural_presence_and_sha256",
        "quality_review": "not_assessed",
        "value": value,
        "evidence": [path for path in available],
        "reason": "no_tracked_candidate_found" if not present else ("candidate_read_failed" if failures else None),
        "unavailable_reasons": failures,
    }


def _social_preview_format(content: bytes) -> str | None:
    if content.startswith(_SOCIAL_PREVIEW_FORMATS["png"]):
        return "png"
    if content.startswith(_SOCIAL_PREVIEW_FORMATS["jpeg"]):
        return "jpeg"
    if any(content.startswith(signature) for signature in _SOCIAL_PREVIEW_FORMATS["gif"]):
        return "gif"
    return None


def _inspect_social_preview(root: Path, relative_path: str) -> dict[str, Any]:
    normalized, _ = _relative_parts(relative_path)
    try:
        content, _ = _read_regular_file(root, normalized, _MAX_SOCIAL_PREVIEW_BYTES - 1)
    except _EvidenceFailure as exc:
        return {
            "validation": "invalid",
            "reason": exc.reason,
            "relative_path": None,
            "sha256": None,
            "size_bytes": None,
            "format": None,
        }
    image_format = _social_preview_format(content)
    if image_format is None:
        return {
            "validation": "invalid",
            "reason": "unsupported_image_format",
            "relative_path": None,
            "sha256": None,
            "size_bytes": None,
            "format": None,
        }
    return {
        "validation": "valid",
        "reason": None,
        "relative_path": normalized,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "format": image_format,
    }


def _social_preview_action(root: Path, action: str | None, asset: str | None) -> dict[str, Any]:
    empty = {
        "evidence_scope": "local_file_only",
        "relative_path": None,
        "sha256": None,
        "size_bytes": None,
        "format": None,
    }
    if action is None:
        if asset is not None:
            return {"action": None, "validation": "invalid", "reason": "asset_without_action", **empty}
        return {"action": None, "validation": "not_requested", "reason": None, **empty}
    if action == "absent":
        if asset is not None:
            return {"action": action, "validation": "invalid", "reason": "asset_with_absent_action", **empty}
        return {
            "action": action,
            "validation": "valid",
            "reason": None,
            "handoff": "remove_in_settings",
            **empty,
            "evidence_scope": "ui_handoff_only",
        }
    if not isinstance(action, str) or action not in {"present", "absent"}:
        return {"action": "invalid", "validation": "invalid", "reason": "action_unsupported", **empty}
    if asset is None:
        return {"action": action, "validation": "invalid", "reason": "asset_required_for_present_action", **empty}
    if action != "present":
        return {"action": action, "validation": "invalid", "reason": "action_unsupported", **empty}
    try:
        result = _inspect_social_preview(root, asset)
    except _EvidenceFailure as exc:
        return {"action": action, "validation": "invalid", "reason": exc.reason, **empty}
    return {"action": action, "handoff": "upload_in_settings", "evidence_scope": "local_file_only", **result}


def _safe_action(action: Any) -> str | None:
    if action is None or (isinstance(action, str) and action in {"present", "absent"}):
        return action
    return "invalid"


def collect_local_evidence(
    checkout_root: str | os.PathLike[str],
    *,
    social_preview_action: str | None = None,
    social_preview_asset: str | None = None,
) -> dict[str, Any]:
    """Collect structural evidence from fixed tracked paths without exposing their contents."""
    try:
        root = _checkout_root(checkout_root)
        tracked_paths = _tracked_candidates(root)
    except _EvidenceFailure as exc:
        return {
            "supported": False,
            "reason": exc.reason,
            "controls": {},
            "file_hashes": {},
            "social_preview": {"action": _safe_action(social_preview_action), "validation": "unavailable", "reason": exc.reason},
        }

    try:
        original_root_info = root.stat()
    except OSError:
        return {
            "supported": False,
            "reason": "checkout_changed_during_collection",
            "controls": {},
            "file_hashes": {},
            "social_preview": {"action": _safe_action(social_preview_action), "validation": "unavailable", "reason": "checkout_changed_during_collection"},
        }

    file_hashes: dict[str, dict[str, Any]] = {}
    badge_counts: dict[str, int] = {}
    read_failures: dict[str, str] = {}
    inspected_total = 0
    for relative_path in sorted(tracked_paths):
        remaining = _MAX_TOTAL_TRACKED_BYTES - inspected_total
        if remaining <= 0:
            read_failures[relative_path] = "aggregate_size_limit_exceeded"
            continue
        limit = min(_MAX_TRACKED_FILE_BYTES, remaining)
        try:
            content, info = _read_regular_file(root, relative_path, limit)
        except _EvidenceFailure as exc:
            read_failures[relative_path] = exc.reason
            continue
        inspected_total += len(content)
        file_hashes[relative_path] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": info.st_size,
        }
        if relative_path in {"README.md", "README.rst", "README.txt"}:
            badge_counts[relative_path] = len(_BADGE_LINK.findall(content))

    controls = {
        control: _control_record(control, paths, tracked_paths, file_hashes, read_failures, badge_counts)
        for control, paths in sorted(_CONTROL_CANDIDATES.items())
    }
    social_preview = _social_preview_action(root, social_preview_action, social_preview_asset)
    if social_preview.get("validation") == "valid" and social_preview.get("action") == "present":
        relative_path = social_preview["relative_path"]
        file_hashes[relative_path] = {
            "sha256": social_preview["sha256"],
            "size_bytes": social_preview["size_bytes"],
        }
    try:
        final_root_info = root.stat()
        if (original_root_info.st_dev, original_root_info.st_ino) != (final_root_info.st_dev, final_root_info.st_ino):
            raise _EvidenceFailure("checkout_changed_during_collection")
        root_binding = _root_binding(root)
    except (OSError, RuntimeError, ValueError, _EvidenceFailure):
        return {
            "supported": False,
            "reason": "checkout_changed_during_collection",
            "controls": {},
            "file_hashes": {},
            "social_preview": {"action": _safe_action(social_preview_action), "validation": "unavailable", "reason": "checkout_changed_during_collection"},
        }
    return {
        "supported": True,
        "reason": None,
        "root_binding": root_binding,
        "controls": controls,
        "file_hashes": dict(sorted(file_hashes.items())),
        "social_preview": social_preview,
    }


def recheck_social_preview_asset(
    checkout_root: str | os.PathLike[str],
    relative_path: str,
    expected_sha256: str,
    *,
    expected_size: int | None = None,
    expected_format: str | None = None,
) -> dict[str, Any]:
    """Safely re-read the same checkout-relative image and compare its bound identity."""
    try:
        root = _checkout_root(checkout_root)
    except _EvidenceFailure as exc:
        return {"supported": False, "matches": False, "reason": exc.reason}
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        return {"supported": False, "matches": False, "reason": "asset_binding_invalid"}
    if expected_size is not None and (not isinstance(expected_size, int) or isinstance(expected_size, bool) or not 0 < expected_size < _MAX_SOCIAL_PREVIEW_BYTES):
        return {"supported": False, "matches": False, "reason": "asset_binding_invalid"}
    if expected_format is not None and (not isinstance(expected_format, str) or expected_format not in _SOCIAL_PREVIEW_FORMATS):
        return {"supported": False, "matches": False, "reason": "asset_binding_invalid"}
    try:
        current = _inspect_social_preview(root, relative_path)
    except _EvidenceFailure as exc:
        return {"supported": False, "matches": False, "reason": exc.reason}
    if current["validation"] != "valid":
        return {"supported": False, "matches": False, "reason": current["reason"]}
    matches = (
        current["sha256"] == expected_sha256
        and (expected_size is None or current["size_bytes"] == expected_size)
        and (expected_format is None or current["format"] == expected_format)
    )
    return {
        "supported": True,
        "matches": matches,
        "reason": None if matches else "asset_changed",
        "relative_path": current["relative_path"],
        "sha256": current["sha256"],
        "size_bytes": current["size_bytes"],
        "format": current["format"],
    }
