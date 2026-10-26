"""Owned, durable command sessions for observable tmux-backed shell work."""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from typing import Any, Callable, Mapping, Sequence
import uuid

from ..application_profiles.service import ApplicationProfileError, validate_profile, load_tmux_runtime_defaults
from ..schema import validate_json_schema
from .command_backend import TmuxCommandBackend
from .command_state import (
    ShellCommandError, atomic_json, make_owner_binding, owner_fingerprint,
    machine_generation, owner_is_current, owner_liveness, private_root,
    private_root_path, process_info, process_state, read_json,
    session_lock, log_lock, validate_identifier,
)
from .command_native_layout import (
    MAX_DERIVED_WEIGHT,
    MIN_PANE_ROWS as _NATIVE_MIN_PANE_ROWS,
    QUIET_SECONDS as _NATIVE_QUIET_SECONDS,
    default_weights_for_new_operation,
    plan_native_layout,
    reactivation_order,
)
API_VERSION = 1
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
_SESSION_HANDLE = re.compile(r"^lsshell1\.([a-f0-9]{24})\.([a-f0-9]{32})$")
_OP_HANDLE = re.compile(r"^lsopcmd1\.([a-f0-9]{24})\.([a-f0-9]{32})\.([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
_FAMILY_LAYOUT = "family-v1"
_CANVAS_LAYOUT = "canvas-v2"
_NATIVE_CANVAS_LAYOUT = "canvas-v3"
_DEFAULT_LAYOUT_MODE = _NATIVE_CANVAS_LAYOUT
_LEGACY_LAYOUT = "legacy-v1"
_FAMILY_MODES = {_FAMILY_LAYOUT, _CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT}
_RUNNER_PANE_MODES = {_FAMILY_LAYOUT, _NATIVE_CANVAS_LAYOUT}
_MEMBER_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_FAMILY_DEFAULT_ROWS = 24
_FAMILY_MIN_PANE_ROWS = 3
_FAMILY_ANCHOR_ROWS = 1
_AGENT_LINES = 200
_AGENT_OUTPUT_BYTES = 32_768
_FAMILY_LOG_BYTES = 20_000_000


def _family_text_bound(width: int, history: int, max_rows: int) -> int:
    """Conservative tmux 3.4 UTF8_SIZE(21) readable-grid bound, including alt grid."""
    return (history + 3 * max_rows - 1) * ((width + 1) * 21 + 1)


def _family_history_ceiling(runtime: Mapping[str, Any]) -> int:
    layout = runtime["layout"]
    width, rows, budget = (layout["canvas_columns"], layout["max_rows"],
                           layout["retained_text_bytes"])
    if (type(width) is not int or not 80 <= width <= 160
            or type(rows) is not int or rows != 512
            or type(budget) is not int or budget != 20_000_000):
        raise ShellCommandError("family_text_contract_invalid", "The native text budget settings are not qualified.")
    per_row = (width + 1) * 21 + 1
    ceiling = (budget - 1) // per_row - (3 * rows - 1)
    if ceiling < 1 or _family_text_bound(width, ceiling, rows) >= budget:
        raise ShellCommandError("family_text_contract_invalid", "The native text budget cannot fit the canvas.")
    return ceiling


def _family_effective_settings(requested: Mapping[str, Any], runtime: Mapping[str, Any]) -> dict[str, Any]:
    history = min(requested["history_limit"], runtime["layout"]["retained_history_lines"],
                  _family_history_ceiling(runtime))
    return dict(requested) | {"history_limit": history,
                              "log_max_bytes": min(requested["log_max_bytes"], _FAMILY_LOG_BYTES),
                              "status": True, "status_position": "bottom"}


def _canvas_effective_settings(requested: Mapping[str, Any], runtime: Mapping[str, Any],
                               *, native: bool = False) -> dict[str, Any]:
    width = runtime["layout"]["canvas_columns"]
    if type(width) is not int or not 80 <= width <= 500:
        raise ShellCommandError("canvas_text_contract_invalid", "The canvas width is outside the qualified range.")
    # Canvas-v2's rendered view and each canvas-v3 native workload PTY share
    # this per-grid history bound. The primary and alternate grids remain
    # below 20 MB at the widest 512-row viewport; each workload also has its
    # separate bounded durable output capture.
    ceiling = max(1, (20_000_000 - 1) // ((500 + 1) * 21 + 1) - 3 * 512 + 1)
    history = min(requested["history_limit"], ceiling)
    return dict(requested) | {"history_limit": history,
                              "log_max_bytes": min(requested["log_max_bytes"], _FAMILY_LOG_BYTES),
                              "status": native, "status_position": "top" if native else "bottom"}


def _family_like(session: Mapping[str, Any]) -> bool:
    return _layout_mode(session) in _FAMILY_MODES


def _family_max_rows(settings: Mapping[str, Any]) -> int:
    # tmux trims a tenth of the history limit per history collection.  Keep
    # every controlled shrink smaller than that batch for small custom limits.
    return min(512, max(1, int(settings["history_limit"]) // 10))


def _runtime_settings_for_service(record: Mapping[str, Any], source_root: Path) -> Mapping[str, Any]:
    stored = record.get("runtime_settings")
    return stored if isinstance(stored, dict) else load_tmux_runtime_defaults(
        source_root=source_root, name="agent-shell")["settings"]


def _native_canvas_settings(runtime: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        settings = runtime["layout"]["native_canvas"]
    except (KeyError, TypeError):
        return {
            "default_columns": 120, "default_rows": 24, "minimum_pane_rows": 3,
            "anchor_rows": 1, "quiet_seconds": int(_NATIVE_QUIET_SECONDS),
            "automatic_view_min_seconds": 2, "activity_coalesce_ms": 250,
            "resize_debounce_ms": 100, "ratio_step_percent": 10,
        }
    if not isinstance(settings, Mapping):
        raise ShellCommandError("native_canvas_profile_invalid", "The native canvas profile settings are malformed.")
    return dict(settings) | {key: runtime[key] for key in (
        "startup_mode", "inspection_enabled", "inspection_duration_seconds",
        "reflow_debounce_ms", "double_escape_ms", "auto_close_completed",
        "auto_close_delay_seconds") if key in runtime}


def _fresh_native_layout(runtime: Mapping[str, Any]) -> dict[str, Any]:
    settings = _native_canvas_settings(runtime)
    return {
        "mode": str(settings.get("startup_mode", "stable")),
        "grouped_sections": True,
        "selected_operation_id": None, "weights": {},
        "compact_idle": True, "max_sections": int(runtime["layout"].get("max_sections", 8)),
        "operation_order": [], "visible_operation_ids": [],
        "expanded_operation_ids": [], "page_anchor_operation_id": None,
        "activity_snapshot": {}, "divider_drag": None, "overlay_lock": None,
        "expansion_restore": None, "inspection_restore": None,
        "inspection_until_monotonic": None,
        "columns": int(settings.get("default_columns", 120)),
        "rows": int(settings.get("default_rows", 24)),
        "desired_generation": 0, "applied_generation": 0, "page": 0,
        "pending": False, "activity_badge": False,
        "last_automatic_change_monotonic": 0.0,
        "last_automatic_apply_monotonic": 0.0,
        "pending_follow_event": None,
        "last_activity_key": None, "last_resize_monotonic": 0.0,
        "last_activation_monotonic": 0.0,
        "history_reflow": [],
    }


def _native_radio_row(
    session: Mapping[str, Any], *, columns: int,
) -> tuple[str, list[tuple[int, int, str, str | None]]]:
    """Render mode radios and bounded mouse targets in the physical footer."""
    state = _validate_native_layout(session.get("native_layout"))
    if type(columns) is not int or columns < 1:
        return "", []
    limit = max(0, columns - 1)  # Last cell can trigger terminal auto-wrap.
    if state.get("grouped_sections"):
        settings = session.get("runtime_settings", {})
        tokens: list[tuple[str, str, str | None]] = (
            [(("(*)" if state["mode"] == mode else "( )") + mode.title(), "mode", mode)
             for mode in ("stable", "follow", "fixed")]
            if settings.get("toolbar_visible", True) else [])
        if settings.get("toolbar_visible", True):
            tokens.extend((label, action, value) for label, action, value in (
                ("<", "page_step", "-1"), (">", "page_step", "+1"),
                ("[Panes]", "panes_menu", None), ("[Logs]", "show_logs", None),
            ))
        tokens.extend((("[Menu]", "f6", None), ("[Exit]", "exit", None)))
    else:
        tokens = [
            (("(*)" if state["mode"] == mode else "( )") + mode, "mode", mode)
            for mode in ("follow", "stable", "fixed")
        ]
        tokens.extend((label, action, value) for label, action, value in (
            ("<", "page_step", "-1"), (">", "page_step", "+1"),
            ("[panes]", "panes_menu", None), ("[layout]", "layout_menu", None),
            ("[logs]", "show_logs", None), ("[?]", "help", None),
        ))
    pieces: list[str] = []
    segments: list[tuple[int, int, str, str | None]] = []
    position = 0
    for label, action, value in tokens:
        separator = "" if not pieces else " "
        if position + len(separator) + len(label) > limit:
            break
        if separator:
            pieces.append(separator)
            position += len(separator)
        start = position
        pieces.append(label)
        position += len(label)
        segments.append((start, position, action, value))
    count = int(state.get("page_count", 1))
    page_label = f" {state['page'] + 1}/{count}" if count > 1 else ""
    badge = page_label + (" !pending" if state.get("pending") or state.get("activity_badge") else "")
    remaining = limit - position
    if badge and remaining >= len(badge):
        pieces.append(badge)
    return "".join(pieces), segments


def _validate_native_layout(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ShellCommandError("native_layout_unknown", "The native layout record is missing.", effect="unknown")
    mode = value.get("mode")
    if mode not in {"follow", "stable", "fixed"}:
        raise ShellCommandError("native_layout_unknown", "The native layout mode is invalid.", effect="unknown")
    columns, rows = value.get("columns"), value.get("rows")
    desired, applied, page = (value.get("desired_generation"),
                              value.get("applied_generation"), value.get("page"))
    if (type(columns) is not int or not 80 <= columns <= 500
            or type(rows) is not int or not 1 <= rows <= 512
            or type(desired) is not int or desired < 0
            or type(applied) is not int or not 0 <= applied <= desired
            or type(page) is not int or page < 0):
        raise ShellCommandError("native_layout_unknown", "The native layout geometry or generations are invalid.", effect="unknown")
    selected = value.get("selected_operation_id")
    if selected is not None:
        try:
            selected = validate_identifier(selected, "operation_id", maximum=128)
        except ShellCommandError as exc:
            raise ShellCommandError("native_layout_unknown", "The selected native operation is invalid.", effect="unknown") from exc
    for field in ("operation_order", "visible_operation_ids", "expanded_operation_ids"):
        items = value.get(field, [])
        if (not isinstance(items, list) or len(items) > 4096
                or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item)
                       for item in items)
                or len(set(items)) != len(items)):
            raise ShellCommandError("native_layout_unknown", "A native layout operation list is malformed.", effect="unknown")
    anchor = value.get("page_anchor_operation_id")
    if anchor is not None and (not isinstance(anchor, str)
                               or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", anchor)):
        raise ShellCommandError("native_layout_unknown", "The native layout page anchor is malformed.", effect="unknown")
    raw_weights = value.get("weights", {})
    if not isinstance(raw_weights, Mapping) or len(raw_weights) > 4096:
        raise ShellCommandError("native_layout_unknown", "The native operation ratios are invalid.", effect="unknown")
    weights: dict[str, int] = {}
    for operation_id, ratio in raw_weights.items():
        try:
            key = validate_identifier(operation_id, "operation_id", maximum=128)
        except ShellCommandError as exc:
            raise ShellCommandError("native_layout_unknown", "A native operation ratio has an invalid identity.", effect="unknown") from exc
        if type(ratio) is not int or not 1 <= ratio <= MAX_DERIVED_WEIGHT:
            raise ShellCommandError("native_layout_unknown", "A native operation ratio is outside its supported range.", effect="unknown")
        weights[key] = ratio
    drag = value.get("divider_drag")
    if drag is not None and (not isinstance(drag, Mapping)
                             or type(drag.get("started_monotonic")) not in {int, float}
                             or not isinstance(drag.get("prior_weights"), Mapping)):
        raise ShellCommandError("native_layout_unknown", "The native divider drag record is malformed.", effect="unknown")
    overlay = value.get("overlay_lock")
    if overlay is not None and (not isinstance(overlay, Mapping)
                                or not isinstance(overlay.get("token"), str)
                                or type(overlay.get("pid")) is not int
                                or type(overlay.get("starttime")) is not int
                                or type(overlay.get("client_pid")) is not int):
        raise ShellCommandError("native_layout_unknown", "The native overlay protection receipt is malformed.", effect="unknown")
    if overlay is not None and any(key in overlay and type(overlay[key]) is not int
                                   for key in ("client_starttime", "client_tty_nr")):
        raise ShellCommandError("native_layout_unknown", "The native overlay client generation is malformed.", effect="unknown")
    expansion = value.get("expansion_restore")
    if expansion is not None and (not isinstance(expansion, Mapping)
                                  or expansion.get("mode") not in {"follow", "stable", "fixed"}
                                  or not isinstance(expansion.get("visible_operation_ids", []), list)
                                  or not isinstance(expansion.get("weights", {}), Mapping)):
        raise ShellCommandError("native_layout_unknown", "The native expand/restore receipt is malformed.", effect="unknown")
    return dict(value) | {"selected_operation_id": selected, "weights": weights}
_SETTING_KEYS = {
    "mouse", "history_limit", "status_position", "status", "preserve_theme",
    "default_lines", "max_response_bytes", "log_max_bytes",
    "completed_log_retention_days", "idle_grace_seconds",
    "collector_interval_seconds", "observer_pins",
}


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _profile_digest(settings: Mapping[str, Any], profile_name: str = "agent-shell") -> str:
    return _digest({
        "schema_version": 1, "application": "tmux", "name": profile_name,
        "settings": dict(settings),
    })


def _settings(
    value: Mapping[str, Any], *, source_root: Path, profile_name: str,
    profile_base: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SETTING_KEYS:
        raise ShellCommandError("invalid_profile_settings", "A fully resolved tmux agent-shell profile is required.")
    try:
        options = {"base_profile": profile_base} if profile_base is not None else {}
        validated = validate_profile("tmux", profile_name, dict(value), source_root=source_root,
                                     **options)
    except ApplicationProfileError as exc:
        raise ShellCommandError("invalid_profile_settings", "The resolved profile failed its registered schema.") from exc
    result = validated["settings"]
    if set(result) != _SETTING_KEYS:
        raise ShellCommandError("invalid_profile_settings", "The resolved profile is missing required shell settings.")
    return result


def _owner(owner: Mapping[str, Any] | None) -> dict[str, Any]:
    if owner is None:
        return make_owner_binding()
    if not isinstance(owner, Mapping):
        raise ShellCommandError("owner_identity_invalid", "The shell owner binding is malformed.")
    return make_owner_binding(owner_pid=owner.get("pid"), owner_starttime=owner.get("starttime"),
                              agent_id=owner.get("agent_id"))


def _scope(owner: Mapping[str, Any]) -> str:
    identity = {
        "machine_generation": owner["machine_generation"], "host": owner["host"],
        "uid": owner["uid"], "agent_id": owner.get("agent_id"),
    }
    if owner.get("agent_id") is None:
        identity.update(pid=owner["pid"], starttime=owner["starttime"])
    return _digest(identity)[:24]


def _layout_mode(record: Mapping[str, Any]) -> str:
    return str(record.get("layout_mode") or _LEGACY_LAYOUT)


def _family_capacity(viewport_height: int) -> int:
    """Fit the anchor and operations, including one divider between panes."""
    if type(viewport_height) is not int or viewport_height < 1:
        raise ShellCommandError("invalid_viewport", "The family viewport height must be a positive row count.")
    return max(0, (viewport_height + 1) // (_FAMILY_MIN_PANE_ROWS + 1) - 1)


def _trusted_member_slug(value: object) -> str:
    if not isinstance(value, str) or not _MEMBER_SLUG.fullmatch(value):
        raise ShellCommandError("invalid_member_label", "Member labels must be short ASCII slugs without control or tmux format characters.")
    return value


def _trusted_agent_id(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)):
        raise ShellCommandError("invalid_member_agent", "A bounded native agent identity is required.")
    return value


def _label_width(value: str) -> int:
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
               for char in value)


def _label_clip(value: str, cells: int, *, from_end: bool = False) -> str:
    selected: list[str] = []
    used = 0
    for char in reversed(value) if from_end else value:
        width = _label_width(char)
        if used + width > cells:
            break
        selected.append(char)
        used += width
    return "".join(reversed(selected)) if from_end else "".join(selected)


def _pane_label(member_slug: str, operation_id: str, cwd: str, *, root: bool,
                width: int = 120) -> str:
    """Bound the pane border to the canvas without interpreting path text as tmux format."""
    prefix = "root" if root else f"sub {member_slug}"
    available = max(1, min(70 - len(prefix) - 3, width - _label_width(prefix) - 25))
    if len(operation_id) <= available:
        purpose = operation_id
    else:
        suffix = hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:8]
        purpose = operation_id[:max(1, available - 9)] + "-" + suffix
    title = f"{prefix} · {purpose} · "
    # Pane option values are later interpolated by tmux. Render controls and
    # format introducers visibly, without letting a path style the border.
    safe_cwd = "".join("?" if unicodedata.category(char).startswith("C") else
                       "＃" if char == "#" else char for char in cwd)
    budget = max(1, width - 4 - _label_width(title))
    if _label_width(safe_cwd) <= budget:
        return title + safe_cwd
    prefix_cells = min(max(8, budget // 3), budget - 4)
    return title + _label_clip(safe_cwd, prefix_cells) + "..." + _label_clip(
        safe_cwd, budget - prefix_cells - 3, from_end=True)


def _reply(state: str, *, ok: bool = True, effect: str = "none", **fields: Any) -> dict[str, Any]:
    return {"api_version": API_VERSION, "ok": ok, "state": state,
            "effect_certainty": effect, **fields}


def _decode_cursor_output(raw: bytes, maximum: int, *, final: bool) -> tuple[str, int]:
    """Bound UTF-8 output while advancing only past bytes represented in it."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pieces: list[str] = []
    output_bytes = 0
    json_bytes = 0
    consumed = 0
    for offset, value in enumerate(raw, 1):
        emitted = decoder.decode(bytes((value,)))
        if emitted:
            size = len(emitted.encode("utf-8"))
            json_size = len(json.dumps(emitted, ensure_ascii=False).encode("utf-8")) - 2
            if output_bytes + size > maximum or json_bytes + json_size > 48_000:
                break
            pieces.append(emitted)
            output_bytes += size
            json_bytes += json_size
            consumed = offset
    else:
        trailing = decoder.decode(b"", final=final)
        if trailing:
            size = len(trailing.encode("utf-8"))
            json_size = len(json.dumps(trailing, ensure_ascii=False).encode("utf-8")) - 2
            if output_bytes + size <= maximum and json_bytes + json_size <= 48_000:
                pieces.append(trailing)
                consumed = len(raw)
        elif not decoder.getstate()[0]:
            consumed = len(raw)
    return "".join(pieces), consumed


def _decode_tail_output(raw: bytes, maximum: int) -> tuple[str, bool]:
    """Keep the newest complete UTF-8 characters from an explicit tail read."""
    decoded = raw.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    truncated = len(encoded) > maximum
    if truncated:
        decoded = encoded[-maximum:].decode("utf-8", errors="ignore")
    if len(json.dumps(decoded, ensure_ascii=False).encode("utf-8")) <= 48_002:
        return decoded, truncated
    low, high = 0, len(decoded)
    while low < high:
        middle = (low + high) // 2
        if len(json.dumps(decoded[middle:], ensure_ascii=False).encode("utf-8")) <= 48_002:
            high = middle
        else:
            low = middle + 1
    return decoded[low:], True


def _decode_tail_page(raw: bytes, maximum: int) -> tuple[str, int]:
    """Return a bounded suffix and exact number of skipped source bytes."""
    dropped = max(0, len(raw) - maximum)
    raw = raw[dropped:]
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high) // 2
        candidate = raw[middle:].decode("utf-8", errors="replace")
        if (len(candidate.encode("utf-8")) <= maximum
                and len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= 48_002):
            high = middle
        else:
            low = middle + 1
    while low < len(raw) and raw[low] & 0xC0 == 0x80:
        low += 1
    return raw[low:].decode("utf-8", errors="replace"), dropped + low


class ShellCommandService:
    """Own one isolated tmux server and command lifecycle per verified agent."""

    def __init__(
        self,
        state_root: str | os.PathLike[str] | None = None,
        *,
        backend: Any | None = None,
        source_root: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_root = state_root
        self.root = private_root_path(state_root) / "agent-shells"
        self.backend = backend or TmuxCommandBackend()
        self.source_root = Path(source_root).resolve() if source_root is not None else Path(__file__).resolve().parents[3]
        self.clock = clock

    def _prepare_root(self) -> None:
        private_root(self.state_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ShellCommandError("unsafe_state_root", "Agent shell state must be owner-private.")
        self.root.chmod(0o700)

    def capabilities(self) -> dict[str, Any]:
        capability = self.backend.capability()
        return _reply("available" if capability.get("state") == "available" else "unavailable",
                      ok=capability.get("state") == "available", backend=capability)

    def _owners(self, *, create: bool = True) -> Path:
        path = self.root / "owners"
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    def _sessions(self, *, create: bool = True) -> Path:
        path = self.root / "sessions"
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    def _owner_dir(self, scope: str) -> Path:
        path = self._owners() / scope
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def _session_dir(self, scope: str, session_id: str, *, create: bool = False) -> Path:
        scope_dir = self._sessions(create=create) / scope
        if create:
            scope_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            scope_dir.chmod(0o700)
        path = scope_dir / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        return path

    @staticmethod
    def _handle(scope: str, session_id: str) -> str:
        return f"lsshell1.{scope}.{session_id}"

    @staticmethod
    def _op_handle(scope: str, session_id: str, op_id: str) -> str:
        return f"lsopcmd1.{scope}.{session_id}.{op_id}"

    def _parse_handle(self, handle: str) -> tuple[str, str, str | None]:
        if not isinstance(handle, str):
            raise ShellCommandError("invalid_handle", "A shell session or operation handle is required.")
        match = _SESSION_HANDLE.fullmatch(handle)
        if match:
            return match.group(1), match.group(2), None
        match = _OP_HANDLE.fullmatch(handle)
        if match:
            return match.group(1), match.group(2), match.group(3)
        raise ShellCommandError("invalid_handle", "The shell handle is malformed.")

    def _load(self, handle: str) -> tuple[Path, dict[str, Any], str | None]:
        scope, sid, op_id = self._parse_handle(handle)
        directory = self._session_dir(scope, sid)
        if directory.is_symlink() or not directory.is_dir():
            raise ShellCommandError("session_missing", "The exact shell session record is unavailable.")
        record = read_json(directory / "session.json")
        if record.get("handle") != self._handle(scope, sid) or record.get("scope_key") != scope or record.get("session_id") != sid:
            raise ShellCommandError("session_identity_mismatch", "The handle does not match its private session record.", effect="unknown")
        if record.get("machine_generation") != machine_generation():
            raise ShellCommandError("wrong_machine", "This exact shell handle belongs to a different machine generation.")
        return directory, record, op_id

    def _authorize(self, record: Mapping[str, Any], supplied: Mapping[str, Any] | None) -> dict[str, Any]:
        caller = _owner(supplied)
        if (owner_fingerprint(caller) != record.get("owner_fingerprint")
                or not owner_is_current(record.get("owner", {}))):
            raise ShellCommandError("owner_identity_mismatch", "This live caller is not the exact shell session owner.")
        return caller

    def _family_caller(
        self, record: Mapping[str, Any], supplied: Mapping[str, Any] | None,
        *, require_root_live: bool = False,
    ) -> tuple[dict[str, Any], str, str, str]:
        if require_root_live and owner_liveness(record.get("owner", {})) != "live":
            raise ShellCommandError("family_root_not_live", "The family root has exited; new dispatch and input are closed.")
        caller = _owner(supplied)
        fingerprint = owner_fingerprint(caller)
        if (fingerprint == record.get("owner_fingerprint")
                and owner_is_current(record.get("owner", {}))):
            return caller, "root", "root", fingerprint
        members = record.get("members", {})
        if isinstance(members, dict):
            for member in members.values():
                if (isinstance(member, dict)
                        and member.get("owner_fingerprint") == fingerprint
                        and owner_is_current(member.get("owner", {}))):
                    return (caller, "member", str(member.get("label_slug", "member")),
                            fingerprint)
        raise ShellCommandError("family_member_required", "This caller is neither the family root nor an admitted member.")

    def _authorize_operation(
        self, session: Mapping[str, Any], operation: Mapping[str, Any],
        supplied: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], str, str, str]:
        if not _family_like(session):
            caller = self._authorize(session, supplied)
            return caller, "root", "root", owner_fingerprint(caller)
        caller, role, slug, fingerprint = self._family_caller(session, supplied)
        if role == "member" and operation.get("member_fingerprint") != fingerprint:
            raise ShellCommandError("operation_member_mismatch", "A family member can access only its own operations.")
        return caller, role, slug, fingerprint

    @staticmethod
    def _require_family_root_live(session: Mapping[str, Any]) -> None:
        if (_family_like(session)
                and owner_liveness(session.get("owner", {})) != "live"):
            raise ShellCommandError("family_root_not_live", "The family root has exited; new dispatch and input are closed.")

    @staticmethod
    def _human_terminal_for(session: Mapping[str, Any]) -> None:
        """Bound a human observer to this machine account and an interactive TTY."""
        if (session.get("machine_generation") != machine_generation()
                or session.get("owner", {}).get("uid") != os.getuid()
                or not os.isatty(0) or not os.isatty(1)):
            raise ShellCommandError("human_terminal_required", "The exact same-user interactive terminal is required.")

    def _verify(self, record: Mapping[str, Any]) -> dict[str, Any]:
        if (_family_like(record)
                and record.get("qualified_tmux_version") != "tmux 3.4"):
            return {"state": "unknown", "windows": [], "reason": "family tmux version contract missing"}
        if record.get("runtime_settings") is not None and _digest(record["runtime_settings"]) != record.get("runtime_digest"):
            return {"state": "unknown", "windows": [], "reason": "runtime baseline record digest mismatch"}
        if _layout_mode(record) == _NATIVE_CANVAS_LAYOUT:
            inspect = getattr(self.backend, "inspect_canvas_v3", None)
            if not callable(inspect):
                return {"state": "unknown", "windows": [],
                        "reason": "native canvas inspector is unavailable"}
            result = inspect(record)
            if result.get("state") != "ready":
                return result
            identity = result.get("identity", {})
            expected_identity = {
                "handle": record.get("handle"), "session_key": record.get("session_key"),
                "machine_generation": record.get("machine_generation"),
                "owner_fingerprint": record.get("owner_fingerprint"),
            }
            if (not isinstance(identity, Mapping)
                    or any(identity.get(key) != value for key, value in expected_identity.items())):
                return {"state": "unknown", "windows": [],
                        "reason": "native canvas session identity mismatch"}
            return result
        result = self.backend.inspect_session(record)
        if result.get("state") != "ready":
            return result
        opts = result.get("options", {})
        expected = {
            "api": "1", "handle": record["handle"], "key": record["session_key"],
            "machine": record["machine_generation"], "owner": record["owner_fingerprint"],
            "profile": record.get("settings_digest"), "status_window": record.get("status_window_id"),
            "layout": record.get("layout_mode"),
        }
        if any(opts.get(k) != v for k, v in expected.items()):
            return {"state": "unknown", "windows": [], "reason": "native session identity mismatch"}
        if _layout_mode(record) == _CANVAS_LAYOUT:
            status = [window for window in result.get("windows", [])
                      if window.get("window_id") == record.get("status_window_id")]
            renderer_pid = record.get("status_pane_pid")
            try:
                renderer_argv = Path(f"/proc/{renderer_pid}/cmdline").read_bytes().split(b"\0")
            except OSError:
                renderer_argv = []
            if (len(status) != 1 or status[0].get("pane_count") != 1
                    or status[0].get("width") != record.get("canvas_width")
                    or status[0].get("height") != record.get("primary_viewport_height")
                    or status[0]["panes"][0].get("pane_id") != record.get("status_pane_id")
                    or status[0]["panes"][0].get("dead") is not False
                    or status[0]["panes"][0].get("input_off") is not True
                    or status[0]["panes"][0].get("pane_pid") != record.get("status_pane_pid")
                    or (process_info(renderer_pid) or {}).get("starttime") != record.get("status_pane_starttime")
                    or b"ls.core.terminal_ops.command_canvas" not in renderer_argv
                    or str(record.get("session_record_path", "")).encode() not in renderer_argv
                    or result.get("options", {}).get("window_size") != "manual"
                    or result.get("applied_settings", {}).get("pane_history_limit", {}).get(
                        record.get("status_pane_id")) != record["settings"]["history_limit"]
                    or type(status[0]["panes"][0].get("history_size")) is not int
                    or status[0]["panes"][0].get("history_size", 0) > record["settings"]["history_limit"] + status[0]["height"] - 1):
                return {"state": "unknown", "windows": [], "reason": "canvas renderer identity mismatch"}
        if _layout_mode(record) == _FAMILY_LAYOUT:
            runtime = _runtime_settings_for_service(record, self.source_root)
            try:
                width = runtime["layout"]["canvas_columns"]
                ceiling = _family_history_ceiling(runtime)
            except (ShellCommandError, KeyError, TypeError):
                return {"state": "unknown", "windows": [], "reason": "family native text contract mismatch"}
            family_options = result.get("options", {})
            status = [window for window in result.get("windows", [])
                      if window.get("window_id") == record.get("status_window_id")]
            if (record["settings"]["history_limit"] > ceiling
                    or family_options.get("window_size") != "manual"
                    or len(status) != 1 or status[0].get("width") != width
                    or status[0].get("height") != record.get("primary_viewport_height", _FAMILY_DEFAULT_ROWS)
                    or not 1 <= status[0].get("height", 0) <= _family_max_rows(record["settings"])
                    or any(pane.get("width") != width or pane.get("input_off") is not True
                           or pane.get("history_size") is None
                           or pane.get("history_size") > record["settings"]["history_limit"] + _family_max_rows(record["settings"]) - 1
                           for pane in status[0].get("panes", []))
                    or any(limit != record["settings"]["history_limit"]
                           for limit in result.get("applied_settings", {}).get("pane_history_limit", {}).values())):
                return {"state": "unknown", "windows": [], "reason": "family viewport identity mismatch"}
        return result

    def _remove_stale_native(self, record: Mapping[str, Any]) -> bool:
        remover = getattr(self.backend, "remove_stale_endpoint", None)
        if callable(remover):
            return bool(remover(record))
        return not Path(str(record.get("endpoint", ""))).exists()

    def _shell_command(self, *arguments: str) -> str:
        """Keep follow-up commands bound to the selected private state store."""
        argv = ["localsetup", "shell"]
        if self.state_root is not None:
            argv.extend(("--state-root", str(self.root.parent)))
        return shlex.join([*argv, *arguments])

    def _session_reply(self, record: Mapping[str, Any], *, created: bool, joined: bool) -> dict[str, Any]:
        handle = str(record["handle"])
        alias = str(record.get("native_name", ""))
        runtime = (_runtime_settings_for_service(record, self.source_root)
                   if _family_like(record) else None)
        layout = runtime["layout"] if runtime is not None else None
        canvas_width = (record.get("canvas_width") if _layout_mode(record) == _CANVAS_LAYOUT else
                        layout["canvas_columns"] if layout is not None else None)
        history = record["settings"]["history_limit"] if layout is not None else None
        requested_history = record.get("requested_settings", record["settings"])["history_limit"]
        configuration = record.get("configuration_applied") or {}
        return _reply(
            str(record.get("state", "unknown")), effect=str(record.get("effect_certainty", "known")),
            handle=handle, session_handle=handle,
            machine={"host": record.get("host"), "generation": record.get("machine_generation")},
            created=created, joined=joined,
            native_session_name=record.get("server_session_name", record.get("native_name")),
            session_alias=alias,
            startup_message=("Created" if created else "Joined") + " owned shell session " +
                            str(record.get("server_session_name", record.get("native_name", "unknown"))) + ".",
            join_command=self._shell_command("attach", "--session", alias),
            settings_digest=record.get("settings_digest"),
            profile_name=record.get("profile_name", "agent-shell"),
            profile_base=record.get("profile_base"),
            cleanup_state=record.get("cleanup_state", "none"),
            gc_at=record.get("gc_at"),
            previous_cleanup=record.get("previous_cleanup"),
            status_window_id=record.get("status_window_id"),
            layout_mode=_layout_mode(record),
            requested_settings=record.get("requested_settings", record.get("settings")),
            effective_settings=record.get("settings"),
            runtime_settings=record.get("runtime_settings"),
            runtime_warnings=record.get("runtime_warnings", []),
            configuration_state=("pending" if record.get("configuration_pending") else
                                 "applied" if configuration else
                                 "bound" if record.get("configuration_binding") else "unbound"),
            configuration_revision=configuration.get("revision"),
            configuration_digest=configuration.get("digest"),
            configuration_error=record.get("configuration_error"),
            canvas_columns=canvas_width,
            native_text_bound_bytes=(_family_text_bound(canvas_width, history, layout["max_rows"])
                                     if layout is not None and _layout_mode(record) != _NATIVE_CANVAS_LAYOUT else None),
            native_text_budget_bytes=(layout["retained_text_bytes"]
                                      if layout is not None and _layout_mode(record) != _NATIVE_CANVAS_LAYOUT else None),
            history_limit_reason=("native_text_budget" if history < min(requested_history,
                                      layout["retained_history_lines"]) else
                                  "runtime_history_preference" if history < requested_history else "requested_profile")
                                 if layout is not None and _layout_mode(record) != _NATIVE_CANVAS_LAYOUT else None,
            history_safe_max_rows=(512 if _layout_mode(record) == _CANVAS_LAYOUT else
                                   _family_max_rows(record["settings"])
                                   if _family_like(record) and _layout_mode(record) != _NATIVE_CANVAS_LAYOUT else None),
            native_layout=record.get("native_layout") if _layout_mode(record) == _NATIVE_CANVAS_LAYOUT else None,
            desired_generation=(record.get("native_layout", {}).get("desired_generation")
                                if _layout_mode(record) == _NATIVE_CANVAS_LAYOUT else None),
            applied_generation=(record.get("native_layout", {}).get("applied_generation")
                                if _layout_mode(record) == _NATIVE_CANVAS_LAYOUT else None),
            native_history_retention=record.get("native_history_retention")
            if _layout_mode(record) == _NATIVE_CANVAS_LAYOUT else None,
        )

    def _native_canvas_operations(
        self, directory: Path, session: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        operations = self._strict_ops(directory, session)
        result: list[dict[str, Any]] = []
        moment = time.monotonic()
        for operation in operations:
            if operation.get("cleanup_state") == "removed":
                continue
            item = dict(operation)
            paths = self._paths(directory, str(operation["operation_id"]))
            cursor: dict[str, Any] = {}
            try:
                cursor = read_json(paths["cursor"], limit=4096)
            except (OSError, ValueError, ShellCommandError):
                pass
            lease = operation.get("input_lease")
            item["input_required"] = bool(
                isinstance(lease, Mapping) and not lease.get("revoked")
                and type(lease.get("deadline_monotonic")) in {int, float}
                and moment < lease["deadline_monotonic"]
            )
            item["activity_monotonic"] = cursor.get(
                "activity_monotonic", operation.get("activated_monotonic", moment))
            finished = operation.get("finished_at")
            if type(finished) in {int, float} and math.isfinite(finished):
                item["finished_monotonic"] = moment - max(0, self.clock() - finished)
            item["native_min_rows"] = max(
                _NATIVE_MIN_PANE_ROWS,
                int(operation.get("native_min_rows", _NATIVE_MIN_PANE_ROWS))
                if type(operation.get("native_min_rows", _NATIVE_MIN_PANE_ROWS)) is int
                else _NATIVE_MIN_PANE_ROWS,
            )
            result.append(item)
        return result

    def _native_layout_plan(
        self, directory: Path, session: Mapping[str, Any], *, now: float | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        state = _validate_native_layout(session.get("native_layout"))
        operations = self._native_canvas_operations(directory, session)
        return operations, self._plan_native_operations(operations, state, session, now=now)

    @staticmethod
    def _native_title_states(operations: Sequence[Mapping[str, Any]], *, now: float,
                             quiet_seconds: float) -> dict[str, str]:
        states: dict[str, str] = {}
        for operation in operations:
            operation_id = str(operation["operation_id"])
            if operation.get("input_required"):
                label = "Input needed"
            elif operation.get("state") == "draining" or operation.get("output_drain") == "pending" and operation.get("state") in {"completed", "failed"}:
                label = "Finishing"
            elif operation.get("state") == "completed":
                label = "Completed"
            elif operation.get("state") == "failed":
                label = "Failed"
            elif operation.get("state") == "running":
                activity = operation.get("activity_monotonic", now)
                label = ("Running" if type(activity) in {int, float} and now - float(activity) < quiet_seconds
                         else "Idle")
            else:
                label = "Idle"
            states[operation_id] = label
        return states

    def _plan_native_operations(self, operations, state, session, *, now=None):
        settings = _native_canvas_settings(_runtime_settings_for_service(session, self.source_root))
        return plan_native_layout(
            operations, state, now=time.monotonic() if now is None else now,
            quiet_seconds=float(settings.get("quiet_seconds", _NATIVE_QUIET_SECONDS)),
            min_pane_rows=int(settings.get("minimum_pane_rows", _NATIVE_MIN_PANE_ROWS)),
            anchor_rows=int(settings.get("anchor_rows", 1)),
            completed_hide_seconds=int(_runtime_settings_for_service(
                session, self.source_root).get("completed_hide_seconds", 60)),
        )

    @staticmethod
    def _native_plan_readback_matches(
        observed: Mapping[str, Any], plan: Mapping[str, Any],
    ) -> bool:
        if (observed.get("state") != "ready"
                or observed.get("applied_generation") != plan.get("generation")):
            return False
        geometry = observed.get("geometry")
        viewport = plan.get("viewport")
        if (not isinstance(geometry, Mapping) or not isinstance(viewport, Mapping)
                or geometry.get("columns") != viewport.get("columns")
                or geometry.get("rows") != viewport.get("rows")):
            return False
        windows = observed.get("windows")
        status_id = geometry.get("status_window_id")
        if not isinstance(windows, list):
            return False
        status_panes = [pane for window in windows if isinstance(window, Mapping)
                        and window.get("window_id") == status_id
                        for pane in window.get("panes", [])
                        if isinstance(pane, Mapping) and pane.get("role") == "status"]
        if (len(status_panes) != 1
                or (plan.get("assignments")
                    and any(item.get("visible") is True for item in plan["assignments"])
                    and status_panes[0].get("height") != plan.get("anchor_rows", 1))
                or status_panes[0].get("y", -1) + status_panes[0].get("height", -1)
                != viewport.get("rows")):
            return False
        summaries = [pane for window in windows if isinstance(window, Mapping)
                     and window.get("window_id") == status_id
                     for pane in window.get("panes", [])
                     if isinstance(pane, Mapping) and pane.get("role") == "summary"]
        collapsed = plan.get("collapsed_sections", [])
        if plan.get("sections"):
            all_panes = [pane for window in windows if isinstance(window, Mapping)
                         and window.get("window_id") == status_id
                         for pane in window.get("panes", []) if isinstance(pane, Mapping)]
            actual = [(pane.get("role"), pane.get("operation_id"), pane.get("height"))
                      for pane in sorted(all_panes, key=lambda item: item.get("y", -1))
                      if pane.get("role") in {"operation", "summary"}]
            expected = [("summary", None, item["rows"]) if item["kind"] == "summary"
                        else ("operation", item["operation_id"], item["rows"])
                        for item in plan["sections"]]
            if actual != expected:
                return False
        elif ((bool(collapsed) != bool(summaries))
              or collapsed and (len(summaries) != 1 or summaries[0].get("height") != len(collapsed)
                                or summaries[0].get("y") != 1)):
            return False
        generation = str(plan.get("generation"))
        if any(window.get("metadata", {}).get("generation") != generation
               or any(pane.get("generation") != generation
                      for pane in window.get("panes", []))
               for window in windows):
            return False
        assignments = plan.get("assignments")
        controls = observed.get("controls")
        rects = controls.get("panes") if isinstance(controls, Mapping) else None
        if not isinstance(assignments, list) or not isinstance(rects, list):
            return False
        visible = [item for item in assignments
                   if isinstance(item, Mapping) and item.get("visible") is True]
        expected_order = [str(item.get("operation_id")) for item in visible]
        if geometry.get("visible_operation_ids") != expected_order:
            return False
        actual_rows = {str(item.get("operation_id")): item.get("rows")
                       for item in rects if isinstance(item, Mapping)}
        return (len(actual_rows) == len(rects) == len(expected_order)
                and all(actual_rows.get(str(item.get("operation_id"))) == item.get("rows")
                        for item in visible))

    def _apply_native_layout_locked(
        self, directory: Path, session: dict[str, Any], *, force: bool = False,
    ) -> dict[str, Any]:
        if _layout_mode(session) != _NATIVE_CANVAS_LAYOUT:
            raise ShellCommandError("native_layout_unsupported", "This session does not use the native canvas.")
        state = _validate_native_layout(session.get("native_layout"))
        backend_apply = getattr(self.backend, "apply_native_layout", None)
        if not callable(backend_apply):
            raise ShellCommandError("native_canvas_unqualified", "The native canvas layout backend is unavailable.")
        observed = self._verify(session)
        if observed.get("state") != "ready":
            raise ShellCommandError("session_identity_unknown", "The exact native canvas inventory is unavailable.", effect="unknown")
        operations = self._native_canvas_operations(directory, session)
        expected_inventory = self._native_inventory(session, observed, operations, allow_active=True)
        if state.get("layout_state") in {"applying", "dispatching", "unknown"}:
            readback_generation = observed.get("applied_generation")
            requested_plan = state.get("requested_plan")
            in_flight_generation = (requested_plan.get("generation")
                                    if isinstance(requested_plan, Mapping) else None)
            if (type(in_flight_generation) is int
                    and readback_generation == in_flight_generation
                    and self._native_plan_readback_matches(observed, requested_plan)):
                newer_desire = state["desired_generation"] > in_flight_generation
                reconciled_signature = _digest({
                    key: value for key, value in requested_plan.items()
                    if key != "generation"})
                state.update(applied_generation=in_flight_generation,
                             pending=newer_desire, activity_badge=newer_desire,
                             layout_state="desired" if newer_desire else "applied",
                             applied_plan_digest=_digest(requested_plan),
                             applied_plan_signature=reconciled_signature,
                             page=requested_plan.get("page", state.get("page", 0)),
                             page_count=requested_plan.get("page_count", state.get("page_count", 1)),
                             collapsed_sections=requested_plan.get("collapsed_sections", []),
                             collapsed_groups=requested_plan.get("collapsed_groups", []),
                             layout_error=None, requested_plan=None,
                             requested_plan_digest=None)
                session["native_layout"] = state
                session["native_control_badge"] = "Activity pending" if newer_desire else ""
                atomic_json(directory / "session.json", session)
                return _reply("pending" if newer_desire else "applied", effect="known",
                              handle=session["handle"],
                              desired_generation=state["desired_generation"],
                              applied_generation=state["applied_generation"],
                              reconciled=True)
            state.update(layout_state="unknown", pending=True,
                         layout_error=state.get("layout_error") or "layout_ack_unknown")
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return _reply("unknown", ok=False, effect="unknown", handle=session["handle"],
                          desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"],
                          error=state.get("layout_error"))
        controls = observed.get("controls", {})
        drag = state.get("divider_drag")
        overlay = state.get("overlay_lock")
        overlay_live = False
        if isinstance(overlay, Mapping):
            process = process_info(overlay.get("pid"))
            overlay_live = bool(process is not None
                                and process.get("starttime") == overlay.get("starttime")
                                and self._native_primary_client(directory, session,
                                                               int(overlay.get("client_pid", 0))))
            if not overlay_live:
                closed = self._native_close_overlay_locked(directory, session, state)
                if not closed:
                    state = _validate_native_layout(session.get("native_layout"))
                    state.update(pending=True, activity_badge=True,
                                 layout_state="overlay_cleanup_pending")
                    session["native_layout"] = state
                    session["native_control_badge"] = "Close the operation overlay to resume layout"
                    atomic_json(directory / "session.json", session)
        frozen = bool(
            isinstance(controls, Mapping)
            and (controls.get("copy_mode") is True or controls.get("overlay") is True
                 or controls.get("input_active") is True or controls.get("zoomed") is True
                 or controls.get("freeze_layout") is True)
        ) or drag is not None or overlay_live or (
            isinstance(state.get("overlay_lock"), Mapping)
            and state["overlay_lock"].get("cleanup_state") in {"dispatching", "unknown", "pending"}
        )
        operations, plan = self._native_layout_plan(directory, session)
        plan_signature = _digest({key: value for key, value in plan.items() if key != "generation"})
        if (state["desired_generation"] == state["applied_generation"]
                and state.get("applied_plan_signature") != plan_signature):
            state["desired_generation"] += 1
            state["pending"] = True
            state["layout_state"] = "desired"
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            operations, plan = self._native_layout_plan(directory, session)
            plan_signature = _digest({key: value for key, value in plan.items() if key != "generation"})
        if plan.get("blocked"):
            state.update(pending=True,
                         activity_badge=True,
                         layout_state="blocked",
                         layout_error=plan["blocked"], page=plan.get("page", 0))
            session["native_layout"] = state
            session["native_control_badge"] = "Resize for minimum pane height"
            session["last_activity_kind"] = "layout_blocked"
            atomic_json(directory / "session.json", session)
            return _reply("pending", effect="none", handle=session["handle"],
                          layout_error=plan["blocked"], desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"],
                          activity_badge=state["activity_badge"])
        if frozen:
            state.update(pending=(state["desired_generation"] > state["applied_generation"]),
                         activity_badge=True, layout_state="frozen",
                         page=plan.get("page", state.get("page", 0)))
            session["native_layout"] = state
            session["native_control_badge"] = "Activity pending"
            atomic_json(directory / "session.json", session)
            return _reply("pending", effect="known", handle=session["handle"],
                          desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"],
                          activity_badge=True)
        plan_digest = _digest(plan)
        if (not force and state["desired_generation"] == state["applied_generation"]
                and state.get("applied_plan_signature") == plan_signature):
            return _reply("applied", effect="known", handle=session["handle"],
                          desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"],
                          page=state.get("page", 0), page_count=plan.get("page_count", 1),
                          unchanged=True)
        state.update(pending=True, activity_badge=False, layout_state="applying",
                     requested_plan_digest=plan_digest, requested_plan=plan,
                     page=plan["page"])
        visible = [item["operation_id"] for item in plan["assignments"] if item["visible"]]
        page_ids = plan.get("page_operation_ids", visible)
        state["page_anchor_operation_id"] = page_ids[0] if page_ids else None
        session["native_layout"] = state
        session["native_control_badge"] = ""
        atomic_json(directory / "session.json", session)
        try:
            result = backend_apply(session, plan, expected_inventory)
        except ShellCommandError as exc:
            if exc.effect == "unknown":
                state.update(pending=True, layout_state="unknown", layout_error=exc.code)
                session["native_layout"] = state
                atomic_json(directory / "session.json", session)
            else:
                state.update(pending=True, layout_state="desired", layout_error=exc.code)
                session["native_layout"] = state
                atomic_json(directory / "session.json", session)
            raise
        if result.get("state") in {"frozen", "deferred", "pending"}:
            state.update(pending=True, activity_badge=True, layout_state="frozen")
            session["native_layout"] = state
            session["native_control_badge"] = "Activity pending"
            atomic_json(directory / "session.json", session)
            return _reply("pending", effect="known", handle=session["handle"],
                          desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"], activity_badge=True)
        if (result.get("state") not in {"applied", "unchanged"}
                or result.get("applied_generation") != plan["generation"]):
            state.update(pending=True, layout_state="unknown",
                         layout_error=str(result.get("reason", "native_layout_readback_unknown")))
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            raise ShellCommandError("native_layout_unknown", "The requested native layout did not read back at its desired generation.", effect="unknown")
        try:
            # The backend's result is its complete post-mutation inspect, with
            # the outcome label replacing the inspection state. Restore the
            # inspection state for the inventory validator; retain the outcome
            # label in the result and require its exact generation above.
            self._native_inventory(session, dict(result) | {"state": "ready"},
                                   operations, allow_active=True)
        except ShellCommandError as exc:
            state.update(pending=True, layout_state="unknown", layout_error=exc.code)
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            raise ShellCommandError(exc.code, exc.message, effect="unknown") from exc
        retention = result.get("history_retention")
        session["native_history_retention"] = (
            dict(retention) if isinstance(retention, Mapping) else
            {"state": "not_reported", "reason": "backend omitted native history readback"}
        )
        state.update(applied_generation=plan["generation"], pending=False,
                     activity_badge=False, layout_state="applied",
                     applied_plan_digest=plan_digest,
                     applied_plan_signature=plan_signature, page=plan["page"],
                     page_count=plan["page_count"], last_applied_at=self.clock())
        state["collapsed_sections"] = plan.get("collapsed_sections", [])
        state["collapsed_groups"] = plan.get("collapsed_groups", [])
        if isinstance(retention, Mapping) and retention.get("state") in {"limit", "reflow"}:
            state["retention_badge"] = "Native history retention changed"
        else:
            state.pop("retention_badge", None)
        session["native_layout"] = state
        session["native_control_badge"] = state.get("retention_badge", "")
        session["last_activity_at"] = self.clock()
        session["last_activity_kind"] = "native_layout_applied"
        atomic_json(directory / "session.json", session)
        return _reply("applied", effect="known", handle=session["handle"],
                      desired_generation=state["desired_generation"],
                      applied_generation=state["applied_generation"],
                      page=state["page"], page_count=state["page_count"],
                      native_history_retention=session["native_history_retention"],
                      inventory=result.get("inventory"))

    @staticmethod
    def _native_ratio_values(
        ratios: Mapping[str, object], known_ids: set[str], *, derived: bool = False,
    ) -> dict[str, int]:
        if not isinstance(ratios, Mapping) or not ratios or len(ratios) > 4096:
            raise ShellCommandError("invalid_native_ratios", "Supply one or more bounded operation ratios.")
        parsed: dict[str, int] = {}
        percent_sum = 0
        percent_mode: bool | None = None
        for raw_id, raw_value in ratios.items():
            operation_id = validate_identifier(raw_id, "operation_id", maximum=128)
            if operation_id not in known_ids:
                raise ShellCommandError("native_operation_missing", "A ratio names an unknown operation.")
            is_percent = isinstance(raw_value, str) and raw_value.endswith("%")
            if percent_mode is None:
                percent_mode = is_percent
            elif percent_mode != is_percent:
                raise ShellCommandError("invalid_native_ratios", "Use percentages or relative weights in one request.")
            if is_percent:
                if derived:
                    raise ShellCommandError("invalid_native_ratios", "Internal ratio adjustment requires integer weights.")
                number = raw_value[:-1]
                if not re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,2})?", number):
                    raise ShellCommandError("invalid_native_ratios", "Percentages must be from 0.01% through 100%.")
                value = int(round(float(number) * 100))
                if not 1 <= value <= 10_000:
                    raise ShellCommandError("invalid_native_ratios", "Percentages must be from 0.01% through 100%.")
                percent_sum += value
            else:
                upper = MAX_DERIVED_WEIGHT if derived else 10_000
                if type(raw_value) is not int or not 1 <= raw_value <= upper:
                    raise ShellCommandError("invalid_native_ratios", f"Weights must be integers from 1 through {upper}.")
                value = raw_value
            parsed[operation_id] = value
        if percent_mode and percent_sum != 10_000:
            raise ShellCommandError("invalid_native_ratios", "Percentages must sum to 100%.")
        return parsed

    def _change_native_layout_locked(
        self, directory: Path, session: dict[str, Any], operations: Sequence[Mapping[str, Any]],
        *, mode: str | None = None, selected_operation_id: str | None = None,
        ratios: Mapping[str, object] | None = None, page: int | None = None,
        expand_operation_id: str | None = None, restore_operation_id: str | None = None,
        derived_ratio_adjustment: bool = False, defer: bool = False,
    ) -> dict[str, Any]:
        state = _validate_native_layout(session.get("native_layout"))
        ids = {str(item["operation_id"]) for item in operations}
        if page is not None and mode is None and state["mode"] == "follow":
            mode = "stable"
        if mode is not None and mode not in {"follow", "stable", "fixed"}:
            raise ShellCommandError("invalid_native_layout_mode", "Layout mode must be follow, stable, or fixed.")
        before = _digest(state)
        current_plan = self._plan_native_operations(operations, state, session)
        if mode == "stable" and state["mode"] == "follow":
            state["visible_operation_ids"] = list(current_plan.get("candidate_operation_ids", []))
        elif mode == "fixed" and not state.get("visible_operation_ids"):
            state["visible_operation_ids"] = list(current_plan.get("candidate_operation_ids", []))
        if mode is not None:
            state["mode"] = mode
            state["manual_page"] = False
        if selected_operation_id is not None:
            selected_operation_id = validate_identifier(selected_operation_id, "operation_id", maximum=128)
            if selected_operation_id not in ids:
                raise ShellCommandError("native_operation_missing", "The selected operation does not exist.")
            state["selected_operation_id"] = selected_operation_id
            state["mode"] = "stable"
            visible = list(state.get("visible_operation_ids", []))
            if selected_operation_id not in visible:
                visible.append(selected_operation_id)
            state["visible_operation_ids"] = visible
            state["page_anchor_operation_id"] = selected_operation_id
            state["expanded_operation_ids"] = list(dict.fromkeys(
                [*state.get("expanded_operation_ids", []), selected_operation_id]))
        if ratios is not None:
            parsed = self._native_ratio_values(ratios, ids, derived=derived_ratio_adjustment)
            state["weights"].update(parsed)
            visible = list(state.get("visible_operation_ids", []))
            for operation_id in parsed:
                if operation_id not in visible:
                    visible.append(operation_id)
            state["visible_operation_ids"] = visible
            state["mode"] = "fixed"
        if expand_operation_id is not None or restore_operation_id is not None:
            target = expand_operation_id or restore_operation_id
            assert target is not None
            target = validate_identifier(target, "operation_id", maximum=128)
            if target not in ids:
                raise ShellCommandError("native_operation_missing", "The operation to expand or restore does not exist.")
            if expand_operation_id is not None:
                state["expanded_operation_ids"] = list(dict.fromkeys(
                    [*state.get("expanded_operation_ids", []), target]))
                if state.get("expansion_restore") is None:
                    state["expansion_restore"] = {
                        "mode": state["mode"], "weights": dict(state["weights"]),
                        "visible_operation_ids": list(state.get("visible_operation_ids", [])),
                        "page": state["page"],
                        "page_anchor_operation_id": state.get("page_anchor_operation_id"),
                    }
                state["mode"] = "stable"
                state["visible_operation_ids"] = [target]
                state["page_anchor_operation_id"] = target
            elif isinstance(state.get("expansion_restore"), Mapping):
                state.update(dict(state["expansion_restore"]))
                state["expansion_restore"] = None
            if restore_operation_id is not None:
                state["expanded_operation_ids"] = [item for item in state.get("expanded_operation_ids", [])
                                                   if item != target]
                state["visible_operation_ids"] = [item for item in state.get("visible_operation_ids", [])
                                                  if item != target]
                if state.get("selected_operation_id") == target:
                    state["selected_operation_id"] = None
        if page is not None:
            if type(page) is not int or page < 0:
                raise ShellCommandError("invalid_native_page", "Page must be a non-negative integer.")
            state["page"] = page
            state["manual_page"] = True
            state["page_anchor_operation_id"] = None
            page_plan = self._plan_native_operations(operations, state, session)
            if page >= page_plan["page_count"]:
                raise ShellCommandError("invalid_native_page", "The requested page is outside the current layout.")
            # Browsing an all-collapsed page must not replace the last active
            # operation, which remains expanded on its own page.
        if state["mode"] in {"stable", "fixed"}:
            state["visible_operation_ids"] = [item for item in state.get("visible_operation_ids", []) if item in ids]
        if _digest(state) != before:
            state["desired_generation"] += 1
            state["pending"] = True
            if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
                state["layout_state"] = "desired"
        session["native_layout"] = state
        if defer and state["desired_generation"] > state["applied_generation"]:
            state["manual_change_pending"] = True
            state["manual_due_monotonic"] = time.monotonic() + int(
                _native_canvas_settings(_runtime_settings_for_service(
                    session, self.source_root)).get("reflow_debounce_ms", 100)) / 1000
            session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        if defer:
            return _reply("pending" if state["pending"] else "unchanged", effect="known",
                          handle=session["handle"],
                          desired_generation=state["desired_generation"],
                          applied_generation=state["applied_generation"])
        result = self._apply_native_layout_locked(directory, session)
        if result.get("page_count") is not None and page is not None and page >= result["page_count"]:
            raise ShellCommandError("invalid_native_page", "The requested page is outside the current layout.")
        return result

    def _begin_native_inspection_locked(
        self, directory: Path, session: dict[str, Any],
        operations: Sequence[Mapping[str, Any]], target: str, *, defer: bool = False,
    ) -> dict[str, Any]:
        state = _validate_native_layout(session.get("native_layout"))
        if target not in {item["operation_id"] for item in operations}:
            raise ShellCommandError("native_operation_missing", "The pane to inspect no longer exists.")
        if not state.get("grouped_sections"):
            return self._change_native_layout_locked(directory, session, operations,
                                                     selected_operation_id=target, defer=defer)
        if _native_canvas_settings(_runtime_settings_for_service(
                session, self.source_root)).get("inspection_enabled", True) is False:
            return self._change_native_layout_locked(directory, session, operations,
                                                     selected_operation_id=target, defer=defer)
        original = state.get("inspection_restore")
        if not isinstance(original, Mapping):
            original = {key: state.get(key) for key in (
                "mode", "page", "manual_page", "selected_operation_id",
                "page_anchor_operation_id", "visible_operation_ids", "weights",
                "expanded_operation_ids")}
        current = self._plan_native_operations(operations, state, session)
        order = [item["operation_id"] for item in current["assignments"]]
        target_index = order.index(target)
        above = [item for item in current["assignments"]
                 if item["visible"] and order.index(item["operation_id"]) < target_index]
        state["inspection_restore"] = original
        state["inspection_target_id"] = target
        state["inspection_preserved_rows"] = {item["operation_id"]: item["rows"] for item in above}
        state["inspection_until_monotonic"] = time.monotonic() + float(
            _native_canvas_settings(_runtime_settings_for_service(session, self.source_root)).get(
                "inspection_duration_seconds", 15))
        state["mode"] = "stable"
        state["visible_operation_ids"] = [item["operation_id"] for item in above] + [target]
        state["selected_operation_id"] = target
        state["page_anchor_operation_id"] = target
        state["manual_page"] = False
        state["desired_generation"] += 1
        state["pending"] = True
        if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
            state["layout_state"] = "desired"
        if defer:
            state["manual_change_pending"] = True
            state["manual_due_monotonic"] = time.monotonic() + int(
                _native_canvas_settings(_runtime_settings_for_service(
                    session, self.source_root)).get("reflow_debounce_ms", 100)) / 1000
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        if defer:
            return _reply("pending", effect="known", handle=session["handle"])
        return self._apply_native_layout_locked(directory, session)

    def _restore_native_inspection_locked(
        self, directory: Path, session: dict[str, Any], *, defer: bool = False,
    ) -> dict[str, Any]:
        state = _validate_native_layout(session.get("native_layout"))
        saved = state.get("inspection_restore")
        if not isinstance(saved, Mapping):
            return _reply("unchanged", effect="none", handle=session["handle"])
        state.update(saved)
        for key in ("inspection_restore", "inspection_target_id",
                    "inspection_preserved_rows", "inspection_until_monotonic"):
            state[key] = None
        state["desired_generation"] += 1
        state["pending"] = True
        if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
            state["layout_state"] = "desired"
        if defer:
            state["manual_change_pending"] = True
            state["manual_due_monotonic"] = time.monotonic() + int(
                _native_canvas_settings(_runtime_settings_for_service(
                    session, self.source_root)).get("reflow_debounce_ms", 100)) / 1000
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        if defer:
            return _reply("pending", effect="known", handle=session["handle"])
        return self._apply_native_layout_locked(directory, session)

    def _native_inspection_tick_locked(
        self, directory: Path, session: dict[str, Any], *, now: float,
        controls: Mapping[str, Any],
    ) -> bool:
        """Extend reading time or restore a temporary inspection once."""
        state = _validate_native_layout(session.get("native_layout"))
        if state.get("inspection_restore") is None:
            return False
        expiry = state.get("inspection_until_monotonic")
        reading = (controls.get("copy_mode") is True
                   or state.get("overlay_lock") is not None
                   or state.get("divider_drag") is not None)
        if reading:
            state["inspection_until_monotonic"] = now + float(
                _native_canvas_settings(_runtime_settings_for_service(
                    session, self.source_root)).get("inspection_duration_seconds", 15))
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return False
        if type(expiry) in {int, float} and now >= float(expiry):
            self._restore_native_inspection_locked(directory, session, defer=True)
            return True
        return False

    def refresh_ui(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reload owned controls without replacing workload processes or viewers."""
        from .command_ui_refresh import refresh_native_renderers
        return refresh_native_renderers(self, handle, owner=owner)

    def layout(
        self, handle: str, *, mode: str | None = None,
        selected_operation_id: str | None = None,
        ratios: Mapping[str, object] | None = None, page: int | None = None,
        expand_operation_id: str | None = None, restore_operation_id: str | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Layout changes require a session handle.")
        if _layout_mode(session) != _NATIVE_CANVAS_LAYOUT:
            raise ShellCommandError("native_layout_unsupported", "The native layout command requires a canvas-v3 session.")
        if _family_like(session):
            _caller, role, _slug, _fingerprint = self._family_caller(session, owner, require_root_live=True)
            if role != "root":
                raise ShellCommandError("native_layout_root_required", "Only the session owner can change the shared native layout.")
        else:
            self._authorize(session, owner)
        with session_lock(directory, blocking=True):
            session = read_json(directory / "session.json")
            self._authorize(session, owner)
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                raise ShellCommandError("session_not_ready", "The native canvas is not ready for layout changes.", effect="unknown")
            operations = self._native_canvas_operations(directory, session)
            self._native_inventory(session, self._verify(session), operations, allow_active=True)
            return self._change_native_layout_locked(
                directory, session, operations, mode=mode,
                selected_operation_id=selected_operation_id, ratios=ratios, page=page,
                expand_operation_id=expand_operation_id,
                restore_operation_id=restore_operation_id,
            )

    def dialog(
        self, handle: str, *, operation_id: str, form: Any, deadline: float,
        validate_current: Callable[[], bool], deliver: Callable[..., bool],
        client_pid: int | None = None, owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .dialog_service import native_dialog

        return native_dialog(self, handle, operation_id=operation_id, form=form, deadline=deadline,
                             validate_current=validate_current, deliver=deliver, client_pid=client_pid, owner=owner)

    def _native_primary_client(
        self, directory: Path, session: Mapping[str, Any], client_pid: int,
    ) -> bool:
        if type(client_pid) is not int or client_pid <= 0:
            return False
        pins, unknown = self._pins(directory, remove_dead=False)
        primary_pins = [pin for pin in pins if pin.get("primary_viewport") is True]
        matching = [pin for pin in primary_pins if pin.get("viewer_pid") == client_pid]
        if unknown or len(primary_pins) != 1 or len(matching) != 1:
            return False
        pin = matching[0]
        owner_process = process_info(pin.get("pid"))
        client = process_info(client_pid)
        if (owner_process is None or owner_process.get("starttime") != pin.get("starttime")
                or client is None or client.get("ppid") != pin.get("pid")):
            return False
        clients = self.backend._clients(session)
        return len([item for item in clients if item.get("pid") == client_pid
                    and "ignore-size" in item.get("flags", set())
                    and "read-only" not in item.get("flags", set())]) == 1

    def _native_viewer_client(
        self, directory: Path, session: Mapping[str, Any], client_pid: int,
    ) -> bool:
        """Verify an attached viewer for exit without granting layout authority."""
        if type(client_pid) is not int or client_pid <= 0:
            return False
        pins, unknown = self._pins(directory, remove_dead=False)
        matching = [pin for pin in pins if pin.get("viewer_pid") == client_pid]
        if unknown or len(matching) != 1:
            return False
        pin = matching[0]
        attach = process_info(pin.get("pid"))
        viewer = process_info(client_pid)
        if (attach is None or viewer is None or attach.get("starttime") != pin.get("starttime")
                or viewer.get("ppid") != pin.get("pid")):
            return False
        clients = self.backend._clients(session)
        return len([item for item in clients if item.get("pid") == client_pid
                    and "ignore-size" in item.get("flags", set())
                    and "read-only" not in item.get("flags", set())]) == 1

    @staticmethod
    def _native_overlay_client_binding(client_pid: int) -> dict[str, int]:
        client = process_info(client_pid)
        if client is None:
            raise ShellCommandError("native_control_primary_required", "The primary client exited before admission.")
        return {"client_starttime": client["starttime"], "client_tty_nr": client["tty_nr"]}

    def _native_open_menu(
        self, path: Path, client_pid: int, *, section: str = "all",
    ) -> dict[str, Any]:
        if section not in {"all", "panes", "layout", "appearance", "reading", "status", "session", "keyboard"}:
            raise ShellCommandError("invalid_native_menu", "The native control menu section is invalid.")
        directory = path.parent
        token = uuid.uuid4().hex
        helper = process_info(os.getpid())
        if helper is None:
            raise ShellCommandError("native_overlay_identity_unknown", "The operation menu helper identity is unavailable.", effect="unknown")
        with session_lock(directory, blocking=True):
            session = read_json(path, limit=2_000_000)
            if (session.get("state") != "ready"
                    or _layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                    or self._verify(session).get("state") != "ready"
                    or not self._native_primary_client(directory, session, client_pid)):
                raise ShellCommandError("native_control_primary_required", "Only the exact primary canvas client can open the operation menu.")
            operations = self._native_canvas_operations(directory, session)
            self._native_inventory(session, self._verify(session), operations, allow_active=True)
            native_state = _validate_native_layout(session.get("native_layout"))
            if native_state.get("overlay_lock") is not None:
                raise ShellCommandError("native_overlay_busy", "The primary client already owns an overlay.", effect="none")
            entries: list[dict[str, Any]] = []
            if section == "all" and native_state.get("grouped_sections"):
                settings = session.get("runtime_settings", {})
                theme = settings.get("theme", {})
                from .command_theme import palette, themes
                theme_name = theme.get("name") if isinstance(theme, Mapping) else None
                if not theme_name:
                    colors = palette(session)
                    theme_name = next((item["name"] for item in themes()
                                       if item["roles"] == colors), "Custom")
                entries.extend([
                    {"label": f"Appearance — {theme_name}; colors and preview", "action": "menu", "operation_id": "appearance"},
                    {"label": f"Reading/layout — {native_state['mode'].title()}; pane behavior", "action": "menu", "operation_id": "reading"},
                    {"label": f"Status detail — {settings.get('status_detail', 'standard')}; header and titles", "action": "menu", "operation_id": "status"},
                    {"label": f"Session behavior — auto-close {'on' if settings.get('auto_close_completed') else 'off'}", "action": "menu", "operation_id": "session"},
                    {"label": "Keyboard help — keys and mouse controls", "action": "menu", "operation_id": "keyboard"},
                ])
            if section in {"all", "panes"} and not (section == "all" and native_state.get("grouped_sections")):
                grouped_states = (self._native_title_states(
                    operations, now=time.monotonic(),
                    quiet_seconds=float(_native_canvas_settings(
                        _runtime_settings_for_service(session, self.source_root)).get(
                            "quiet_seconds", _NATIVE_QUIET_SECONDS)))
                                  if native_state.get("grouped_sections") else {})
                for operation in operations:
                    op_id = str(operation["operation_id"])
                    radio = "(*)" if op_id == native_state.get("selected_operation_id") else "( )"
                    if native_state.get("grouped_sections"):
                        status = grouped_states[op_id]
                        purpose = _label_clip(str(operation.get("pane_label", op_id)),
                                              max(1, 68 - _label_width(status)))
                        label = f"{radio} {purpose} [{status}]"
                    else:
                        label = f"{radio} {op_id} [{operation.get('state', 'unknown')}]"
                        label = label.encode("ascii", "replace").decode("ascii")
                    entries.append({"label": label,
                                    "action": "select", "operation_id": op_id})
            if section in {"all", "layout", "reading"} and not (section == "all" and native_state.get("grouped_sections")):
                entries.extend({
                    "label": ("(*)" if native_state["mode"] == mode else "( )") + mode.title() +
                             (" — active work follows activity" if mode == "follow" else
                              " — even shares, manual placement" if mode == "stable" else
                              " — preserve chosen proportions"),
                    "action": "mode", "operation_id": mode,
                } for mode in ("follow", "stable", "fixed"))
            if section == "appearance":
                from .command_theme import themes
                for theme in themes():
                    name = theme["name"]
                    slug = name.lower().replace(" ", "-")
                    entries.append({"label": f"Preview {name} — inspect colors without applying",
                                    "action": "theme_preview", "operation_id": slug})
                    entries.append({"label": f"Apply {name} — this session only",
                                    "action": "theme", "operation_id": slug})
                entries.append({"label": f"Colors: {'on' if session['runtime_settings'].get('colors_enabled', True) else 'off'}",
                                "action": "setting", "operation_id": "colors_toggle"})
                entries.append({"label": "Make default — save current choices if signed write authority is bound",
                                "action": "make_default"})
            if section == "status":
                for detail in ("minimal", "standard", "detailed"):
                    entries.append({"label": f"{detail.title()} — " +
                                    ("identity only" if detail == "minimal" else
                                     "identity, location, and state" if detail == "standard" else
                                     "identity, location, state, mode, page, and activity"),
                                    "action": "setting", "operation_id": f"status_{detail}"})
            if section == "session":
                settings = session["runtime_settings"]
                entries.extend([
                    {"label": f"Auto-close after owner exit and drained jobs: {'on' if settings.get('auto_close_completed') else 'off'}",
                     "action": "setting", "operation_id": "auto_close_toggle"},
                    *[{"label": f"Hide completed after {seconds}s" +
                        (" (selected)" if settings.get('completed_hide_seconds', 60) == seconds else ""),
                        "action": "setting", "operation_id": f"hide_completed_{seconds}"}
                      for seconds in (30, 60, 120, 300)],
                    {"label": f"Toolbar visibility: {'on' if settings.get('toolbar_visible') else 'off'}",
                     "action": "setting", "operation_id": "toolbar_toggle"},
                    {"label": f"Shortcut hints on the home screen: {'on' if settings.get('shortcut_hints_visible') else 'off'}",
                     "action": "setting", "operation_id": "hints_toggle"},
                    {"label": "Make default — save current choices if signed write authority is bound",
                     "action": "make_default"},
                ])
            if section == "keyboard":
                shortcuts = session.get("runtime_settings", {}).get("native_shortcuts", {})
                entries.append({"label": (f"{shortcuts.get('focus_next', 'Tab')}/{shortcuts.get('focus_previous', 'BTab')} move; "
                                           f"{shortcuts.get('activate', 'Enter')} inspects; "
                                           "Escape twice exits; Ctrl-Q exits"),
                                "action": "help"})
            selected = native_state.get("selected_operation_id")
            if selected is not None:
                if section in {"all", "layout"}:
                    entries.extend([
                        {"label": "More space for selected", "action": "increase_ratio", "operation_id": selected},
                        {"label": "Less space for selected", "action": "decrease_ratio", "operation_id": selected},
                    ])
                if section in {"all", "panes"}:
                    entries.extend([
                        {"label": "Expand selected", "action": "expand", "operation_id": selected},
                        {"label": "Restore selected", "action": "restore", "operation_id": selected},
                    ])
            menu_title = (section.title() if section != "all" else
                          "Menu" if native_state.get("grouped_sections") else f"Controls {native_state['mode']}")
            native_state["overlay_lock"] = {
                "token": token, "client_pid": client_pid,
                **self._native_overlay_client_binding(client_pid),
                "pid": os.getpid(), "starttime": helper["starttime"],
                "kind": "opening", "opened_monotonic": time.monotonic(),
            }
            session["native_layout"] = native_state
            atomic_json(path, session)

        def on_spawn(pid: int, starttime: int) -> None:
            with session_lock(directory, blocking=True):
                current = read_json(path, limit=2_000_000)
                state = _validate_native_layout(current.get("native_layout"))
                lock = state.get("overlay_lock")
                if not isinstance(lock, Mapping) or lock.get("token") != token:
                    raise ShellCommandError("native_overlay_identity_unknown", "The native menu receipt changed before display.", effect="unknown")
                state["overlay_lock"] = dict(lock) | {
                    "pid": pid, "starttime": starttime, "kind": "tmux_client",
                }
                current["native_layout"] = state
                atomic_json(path, current)

        show = getattr(self.backend, "show_native_menu", None)
        if not callable(show):
            with session_lock(directory, blocking=True):
                current = read_json(path, limit=2_000_000)
                state = _validate_native_layout(current.get("native_layout"))
                if state.get("overlay_lock", {}).get("token") == token:
                    state["overlay_lock"] = None
                    current["native_layout"] = state
                    atomic_json(path, current)
            raise ShellCommandError("native_control_unavailable", "The native operation menu is unavailable.")
        outcome: dict[str, Any] = {"state": "dismissed", "ok": True}
        try:
            returned = show(session, client_pid, menu_title, entries,
                            on_spawn=on_spawn)
            if isinstance(returned, Mapping):
                outcome.update(returned)
        finally:
            with session_lock(directory, blocking=True):
                current = read_json(path, limit=2_000_000)
                state = _validate_native_layout(current.get("native_layout"))
                lock = state.get("overlay_lock")
                if isinstance(lock, Mapping) and lock.get("token") == token:
                    closed = self._native_close_overlay_locked(directory, current, state)
                else:
                    closed = True
                if closed and (state.get("pending")
                               or state.get("desired_generation") > state.get("applied_generation")):
                    try:
                        self._apply_native_layout_locked(directory, current)
                    except ShellCommandError as exc:
                        outcome.update(state="pending" if exc.effect != "unknown" else "unknown",
                                       ok=exc.effect != "unknown", error=exc.code)
                elif not closed:
                    outcome.update(state="pending", ok=False,
                                   error=state.get("overlay_lock", {}).get("cleanup_error", "overlay_close_unverified"))
        selected_index = outcome.get("selected_index")
        if (native_state.get("grouped_sections") and closed
                and outcome.get("verified") is True
                and type(selected_index) is int and 0 <= selected_index < len(entries)):
            selected_entry = entries[selected_index]
            return self._native_control_event(
                path, client_pid, str(selected_entry["action"]),
                operation_id=selected_entry.get("operation_id"))
        return _reply(str(outcome.get("state", "dismissed")), ok=outcome.get("ok") is not False,
                      effect=str(outcome.get("effect_certainty", "known")),
                      handle=session.get("handle"), overlay="operation_menu",
                      error=outcome.get("error"))

    def _native_close_overlay_locked(
        self, directory: Path, session: dict[str, Any], state: dict[str, Any],
    ) -> bool:
        lock = state.get("overlay_lock")
        if not isinstance(lock, Mapping):
            return True
        if lock.get("cleanup_state") in {"dispatching", "unknown", "pending"}:
            return False
        dismiss = getattr(self.backend, "dismiss_native_overlay", None)
        if not callable(dismiss):
            state["overlay_lock"] = dict(lock) | {
                "cleanup_state": "unknown", "cleanup_error": "overlay_close_unavailable",
            }
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return False
        state["overlay_lock"] = dict(lock) | {"cleanup_state": "dispatching"}
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        try:
            result = dismiss(
                session, int(lock["client_pid"]), int(lock["pid"]),
                int(lock["starttime"]),
            )
        except ShellCommandError as exc:
            state["overlay_lock"] = dict(lock) | {
                "cleanup_state": "unknown", "cleanup_error": exc.code,
            }
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return False
        if result.get("state") in {"dismissed", "not_open"} and result.get("verified") is True:
            state["overlay_lock"] = None
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return True
        if result.get("state") == "pending" and result.get("reason") == "waiting_client_cleanup":
            state["overlay_lock"] = dict(lock) | {"cleanup_state": "waiting_client_exit"}
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return False
        state["overlay_lock"] = dict(lock) | {
            "cleanup_state": "unknown",
            "cleanup_error": str(result.get("reason", "overlay_close_unverified")),
        }
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        return False

    def _native_open_text_overlay(
        self, path: Path, client_pid: int, title: str, lines: Sequence[str],
        *, preview_theme: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        show = getattr(self.backend, "show_native_overlay", None)
        if not callable(show):
            raise ShellCommandError("native_control_unavailable", "Native text overlays are unavailable.")
        return self._native_open_overlay(path, client_pid, title, lambda session, on_spawn: show(
            (session | {"runtime_settings": dict(session["runtime_settings"]) | {"theme": dict(preview_theme)},
                        "_preview_theme": dict(preview_theme)}
             if preview_theme is not None else session), client_pid, title,
            [str(line).encode("ascii", "replace").decode("ascii") for line in lines][-40:],
            on_spawn=on_spawn))

    def _native_open_overlay(
        self, path: Path, client_pid: int, title: str, show: Callable[..., Mapping[str, Any]],
    ) -> dict[str, Any]:
        directory = path.parent
        token = uuid.uuid4().hex
        dispatched = False
        no_effect_failure = False
        helper = process_info(os.getpid())
        if helper is None:
            raise ShellCommandError("native_overlay_identity_unknown", "The native overlay helper identity is unavailable.", effect="unknown")
        with session_lock(directory, blocking=True):
            session = read_json(path, limit=2_000_000)
            if (session.get("state") != "ready"
                    or _layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                    or self._verify(session).get("state") != "ready"
                    or not self._native_primary_client(directory, session, client_pid)):
                raise ShellCommandError("native_control_primary_required", "Only the exact primary canvas client can open an overlay.")
            operations = self._native_canvas_operations(directory, session)
            self._native_inventory(session, self._verify(session), operations, allow_active=True)
            state = _validate_native_layout(session.get("native_layout"))
            if state.get("overlay_lock") is not None:
                raise ShellCommandError("native_overlay_busy", "The primary client already owns an overlay.", effect="none")
            state["overlay_lock"] = {
                "token": token, "client_pid": client_pid,
                **self._native_overlay_client_binding(client_pid),
                "pid": os.getpid(), "starttime": helper["starttime"],
                "kind": "opening", "opened_monotonic": time.monotonic(),
            }
            session["native_layout"] = state
            atomic_json(path, session)

        def on_spawn(pid: int, starttime: int) -> None:
            nonlocal dispatched
            dispatched = True
            with session_lock(directory, blocking=True):
                current = read_json(path, limit=2_000_000)
                current_state = _validate_native_layout(current.get("native_layout"))
                lock = current_state.get("overlay_lock")
                if not isinstance(lock, Mapping) or lock.get("token") != token:
                    raise ShellCommandError("native_overlay_identity_unknown", "The native overlay receipt changed before display.", effect="unknown")
                current_state["overlay_lock"] = dict(lock) | {
                    "pid": pid, "starttime": starttime, "kind": "tmux_client",
                }
                current["native_layout"] = current_state
                atomic_json(path, current)

        outcome: dict[str, Any] = {"state": "dismissed", "ok": True}
        try:
            returned = show(session, on_spawn)
            if isinstance(returned, Mapping):
                outcome.update(returned)
                if returned.get("verified") is False:
                    outcome.update(ok=False, effect_certainty="unknown")
        except ShellCommandError as exc:
            no_effect_failure = exc.effect == "none" and not dispatched
            raise
        finally:
            with session_lock(directory, blocking=True):
                current = read_json(path, limit=2_000_000)
                state = _validate_native_layout(current.get("native_layout"))
                lock = state.get("overlay_lock")
                if isinstance(lock, Mapping) and lock.get("token") == token:
                    if no_effect_failure:
                        state["overlay_lock"] = None
                        current["native_layout"] = state
                        atomic_json(path, current)
                        closed = True
                    else:
                        closed = self._native_close_overlay_locked(directory, current, state)
                else:
                    closed = lock is None
                if closed and (state.get("pending")
                               or state.get("desired_generation") > state.get("applied_generation")):
                    try:
                        self._apply_native_layout_locked(directory, current)
                    except ShellCommandError as exc:
                        outcome.update(state="pending" if exc.effect != "unknown" else "unknown",
                                       ok=exc.effect != "unknown", error=exc.code)
                elif not closed:
                    outcome.update(state="pending", ok=False,
                                   error=state.get("overlay_lock", {}).get("cleanup_error", "overlay_close_unverified"))
        return _reply(str(outcome.get("state", "dismissed")), ok=outcome.get("ok") is not False,
                      effect=str(outcome.get("effect_certainty", "known")),
                      handle=session.get("handle"), overlay=title,
                      error=outcome.get("error"), cleanup_verified=closed,
                      accepted=outcome.get("accepted") is True)

    def _native_overlay_content(
        self, path: Path, client_pid: int, kind: str,
    ) -> tuple[dict[str, Any], str, list[str]]:
        directory = path.parent
        session = read_json(path, limit=2_000_000)
        with session_lock(directory, blocking=True):
            session = read_json(path, limit=2_000_000)
            if (_layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                    or self._verify(session).get("state") != "ready"
                    or not self._native_primary_client(directory, session, client_pid)):
                raise ShellCommandError("native_control_primary_required", "Only the exact primary canvas client can open an overlay.")
            operations = self._native_canvas_operations(directory, session)
            self._native_inventory(session, self._verify(session), operations, allow_active=True)
            state = _validate_native_layout(session.get("native_layout"))
            if kind == "details":
                selected = state.get("selected_operation_id")
                operation = next((item for item in operations
                                  if item["operation_id"] == selected), None)
                if operation is None:
                    return session, "Pane details", ["No operation is selected."]
                title = str(operation.get("pane_label", selected))
                status = self._native_title_states([operation], now=time.monotonic(),
                    quiet_seconds=float(_native_canvas_settings(
                        _runtime_settings_for_service(session, self.source_root)).get("quiet_seconds", 10)))[selected]
                return session, "Pane details", [
                    f"Purpose and location: {title}", f"State: {status}",
                    f"Operation: {selected}",
                    f"Output drain: {operation.get('output_drain', 'unknown')}",
                    f"Cleanup: {operation.get('cleanup_state', 'unknown')}",
                    f"Layout: {state['mode'].title()}, page {state.get('page', 0) + 1}/{state.get('page_count', 1)}",
                ]
            if kind == "help":
                return session, "Help", [
                    "F6 opens operation radio controls.",
                    "Arrows move; Enter applies; Escape returns to the previous view.",
                    "Layout modes: follow, stable, fixed.",
                    "Use shell layout --handle H --ratio OP=WEIGHT or OP=PERCENT%.",
                    "Use --expand OP or --restore OP to change selected pane height.",
                ]
            if kind == "panes":
                lines = [f"Mode: {state['mode']}  Page: {state.get('page', 0) + 1}/{state.get('page_count', 1)}"]
                for operation in operations:
                    op_id = str(operation["operation_id"])
                    ratio = state.get("weights", {}).get(op_id, 1)
                    radio = "(*)" if op_id == state.get("selected_operation_id") else "( )"
                    lines.append(f"{radio} {op_id}  {operation.get('state', 'unknown')}  weight {ratio}")
                return session, "Panes", lines
            selected = state.get("selected_operation_id")
            if selected is None:
                return session, "Logs", ["No operation is selected."]
            paths = self._paths(directory, selected)
            try:
                with log_lock(paths["cursor"]):
                    with paths["log"].open("rb") as stream:
                        stream.seek(0, os.SEEK_END)
                        stream.seek(max(0, stream.tell() - 16_384))
                        raw = stream.read(16_384)
                output, _skipped = _decode_tail_page(raw, 8192)
            except (OSError, ShellCommandError):
                output = "Output is not currently available."
            return session, f"Logs: {selected}", output.splitlines()[-20:]

    def native_control_event(
        self, session_record: str | os.PathLike[str], client_pid: int, action: str, *,
        operation_id: str | None = None, mouse_x: int | None = None,
        mouse_y: int | None = None, mouse_event: str | None = None,
        mouse_pane: str | None = None,
    ) -> dict[str, Any]:
        result = self._native_control_event(
            session_record, client_pid, action, operation_id=operation_id,
            mouse_x=mouse_x, mouse_y=mouse_y, mouse_event=mouse_event, mouse_pane=mouse_pane)
        if result.get("ok", True) and mouse_event != "drag":
            try:
                result["redraw"] = self._redraw_native_control(Path(session_record), client_pid)
            except ShellCommandError as exc:
                # The option may already have applied. Never replay it because a
                # viewer repaint failed or its acknowledgement was lost.
                result.update(redraw="unknown" if exc.effect == "unknown" else "unavailable",
                              redraw_error=exc.code)
            except OSError:
                result.update(redraw="unknown", redraw_error="native_redraw_failed")
        return result

    def _redraw_native_control(self, path: Path, client_pid: int) -> str:
        with session_lock(path.parent, blocking=True):
            session = read_json(path, limit=2_000_000)
            if (session.get("session_record_path") != str(path)
                    or _layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                    or session.get("state") != "ready"
                    or not self._native_primary_client(path.parent, session, client_pid)):
                raise ShellCommandError("native_control_primary_required", "The redraw viewer is no longer qualified.")
            generation = session.get("ui_redraw_generation", 0)
            if type(generation) is not int or generation < 0:
                raise ShellCommandError("native_control_identity_unknown", "The redraw generation is invalid.")
            session["ui_redraw_generation"] = generation + 1
            atomic_json(path, session)
            if session["native_layout"].get("overlay_lock") is not None:
                return "deferred"
            if session["native_layout"].get("grouped_sections"):
                # The renderer wakes on this record write and paints the
                # requested row even when the selected mode did not change.
                # No extra tmux client refresh is needed for its own pane.
                return "requested"
            redraw = getattr(self.backend, "redraw_native_client", None)
            if not callable(redraw):
                return "unavailable"
            redraw(session, client_pid)
            return "requested"

    def _native_grouped_escape(self, path: Path, client_pid: int, *, pair: bool = False) -> dict[str, Any]:
        """Consume shared transient views before arming this exact viewer's detach."""
        directory = path.parent
        with session_lock(directory, blocking=True):
            session = read_json(path, limit=2_000_000)
            if (session.get("session_record_path") != str(path)
                    or session.get("state") != "ready"
                    or _layout_mode(session) != _NATIVE_CANVAS_LAYOUT):
                raise ShellCommandError("native_control_identity_unknown", "The native control session is not ready.", effect="unknown")
            state = _validate_native_layout(session.get("native_layout"))
            if not state.get("grouped_sections") or not self._native_viewer_client(
                    directory, session, client_pid):
                raise ShellCommandError("native_viewer_required", "Only an exact attached viewer can use Escape.")
            observed = self._verify(session)
            if observed.get("state") != "ready":
                raise ShellCommandError("native_control_identity_unknown", "The exact native control inventory is unavailable.", effect="unknown")
            viewer = process_info(client_pid)
            if viewer is None:
                raise ShellCommandError("native_viewer_required", "The viewer identity changed before Escape.", effect="unknown")
            arms = session.get("native_escape_arms", {})
            if not isinstance(arms, dict):
                raise ShellCommandError("native_control_identity_unknown", "Viewer Escape state is malformed.", effect="unknown")
            arms = dict(arms)
            key = str(client_pid)
            primary = self._native_primary_client(directory, session, client_pid)
            controls = observed.get("controls", {})
            consumed = (isinstance(controls, Mapping) and controls.get("copy_mode") is True)
            overlay = state.get("overlay_lock") is not None
            drag = state.get("divider_drag") is not None
            inspection = state.get("inspection_restore") is not None
            if consumed or overlay or drag or inspection:
                arms.pop(key, None)
                session["native_escape_arms"] = arms
                atomic_json(path, session)
                if consumed or not primary:
                    return _reply("escape_consumed", effect="known", handle=session["handle"])
                if overlay:
                    if self._native_close_overlay_locked(directory, session, state):
                        return _reply("overlay_closed", effect="known", handle=session["handle"])
                    return _reply("overlay_pending", effect="unknown", handle=session["handle"])
                if drag:
                    operations = self._native_canvas_operations(directory, session)
                    self._native_inventory(session, observed, operations, allow_active=True)
                    return self._native_divider_event(
                        directory, session, operations, client_pid, action="escape",
                        mouse_x=None, mouse_y=None, mouse_event=None, mouse_pane=None)
                return self._restore_native_inspection_locked(directory, session, defer=True)
            now = time.monotonic()
            interval = float(_native_canvas_settings(
                _runtime_settings_for_service(session, self.source_root)).get("double_escape_ms", 500)) / 1000
            previous = arms.get(key)
            if (pair or isinstance(previous, Mapping)
                    and previous.get("starttime") == viewer["starttime"]
                    and type(previous.get("at")) in {int, float}
                    and 0 <= now - float(previous["at"]) <= interval):
                arms.pop(key, None)
                session["native_escape_arms"] = arms
                atomic_json(path, session)
                detach = getattr(self.backend, "detach_native_client", None)
                if not callable(detach):
                    raise ShellCommandError("native_control_unavailable", "Exact viewer detach is unavailable.")
                detach(session, client_pid)
                return _reply("detached", effect="known", handle=session["handle"])
            arms[key] = {"starttime": viewer["starttime"], "at": now}
            session["native_escape_arms"] = arms
            atomic_json(path, session)
            return _reply("escape_armed", effect="known", handle=session["handle"])

    def _native_control_event(
        self, session_record: str | os.PathLike[str], client_pid: int, action: str, *,
        operation_id: str | None = None, mouse_x: int | None = None,
        mouse_y: int | None = None, mouse_event: str | None = None,
        mouse_pane: str | None = None,
    ) -> dict[str, Any]:
        path = Path(session_record)
        if not path.is_absolute() or path.name != "session.json":
            raise ShellCommandError("invalid_session_record", "The native control record path is invalid.")
        if action in {"escape", "escape_pair"}:
            candidate = read_json(path, limit=2_000_000)
            if (candidate.get("session_record_path") == str(path)
                    and _layout_mode(candidate) == _NATIVE_CANVAS_LAYOUT
                    and (candidate.get("native_layout") or {}).get("grouped_sections")):
                return self._native_grouped_escape(path, client_pid,
                                                   pair=action == "escape_pair")
        if action in {"menu", "theme", "theme_preview", "setting", "make_default"}:
            deadline = time.monotonic() + .4
            while time.monotonic() < deadline:
                waiting = read_json(path, limit=2_000_000)
                if (waiting.get("native_layout") or {}).get("overlay_lock") is None:
                    break
                time.sleep(.01)
        if action == "menu":
            return self._native_open_menu(path, client_pid, section=str(operation_id))
        if action == "theme_preview":
            from .command_theme import theme_definition, preview
            from .command_live_configuration import ConfigurationError, apply_presentation
            definition = theme_definition(str(operation_id))
            session = read_json(path, limit=2_000_000)
            result = self._native_open_text_overlay(path, client_pid,
                f"Preview: {definition['name']}", [
                    preview(definition, color=False),
                    *[f"{role.title()}: {definition['roles'][role]}" for role in
                      ("background", "surface", "text", "selected", "attention", "success", "error", "running")
                      if role in definition["roles"]],
                    "Enter applies this theme; Escape cancels.",
                ], preview_theme=definition)
            if result.get("accepted") is True:
                if result.get("cleanup_verified") is not True:
                    raise ShellCommandError("native_overlay_cleanup_unknown",
                                            "The preview closed without verified cleanup.", effect="unknown")
                try:
                    applied = apply_presentation(self, session["handle"],
                                                 changes={"theme": definition},
                                                 client_pid=client_pid)
                except ConfigurationError as exc:
                    raise ShellCommandError("native_setting_unavailable", str(exc)) from exc
                return dict(applied) | {"preview_accepted": True}
            return result
        if action in {"theme", "setting", "make_default"}:
            from .command_live_configuration import ConfigurationError, apply_presentation
            from .command_theme import PRESENTATION_KEYS, theme_definition
            current = read_json(path, limit=2_000_000)
            if not self._native_primary_client(path.parent, current, client_pid):
                raise ShellCommandError("native_control_primary_required", "Only the exact primary canvas client can change presentation.")
            settings = current["runtime_settings"]
            try:
                if action == "make_default":
                    changes = {key: settings[key] for key in PRESENTATION_KEYS if key in settings}
                elif action == "theme":
                    changes = {"theme": theme_definition(str(operation_id))}
                else:
                    code = str(operation_id)
                    if code.startswith("status_") and code.removeprefix("status_") in {"minimal", "standard", "detailed"}:
                        changes = {"status_detail": code.removeprefix("status_")}
                    elif code.startswith("hide_completed_"):
                        changes = {"completed_hide_seconds": int(code.removeprefix("hide_completed_"))}
                    elif code in {"colors_toggle", "toolbar_toggle", "auto_close_toggle", "hints_toggle"}:
                        field = {"colors_toggle": "colors_enabled", "toolbar_toggle": "toolbar_visible",
                                 "auto_close_toggle": "auto_close_completed",
                                 "hints_toggle": "shortcut_hints_visible"}[code]
                        changes = {field: not bool(settings.get(field, True))}
                    else:
                        raise ShellCommandError("invalid_native_setting", "The native presentation choice is invalid.")
                outcome = apply_presentation(self, current["handle"], changes=changes,
                                             client_pid=client_pid,
                                             make_default=action == "make_default")
                if action == "make_default":
                    reason = ("Current choices were saved as the signed default."
                              if outcome.get("default_state") == "saved" else
                              str(outcome.get("default_reason", "Current choices apply to this session only.")))
                    return self._native_open_text_overlay(path, client_pid, "Default setting", [reason])
                return outcome
            except (ConfigurationError, ValueError) as exc:
                raise ShellCommandError("native_setting_unavailable", str(exc)) from exc
        if action == "exit":
            session = read_json(path, limit=2_000_000)
            if not self._native_viewer_client(path.parent, session, client_pid):
                raise ShellCommandError("native_viewer_required", "Only an exact attached viewer can use Exit.")
            detach = getattr(self.backend, "detach_native_client", None)
            if not callable(detach):
                raise ShellCommandError("native_control_unavailable", "Exact viewer detach is unavailable.")
            detach(session, client_pid)
            return _reply("detached", effect="known", handle=session["handle"])
        if action == "activate":
            _session, title, lines = self._native_overlay_content(path, client_pid, "details")
            return self._native_open_text_overlay(path, client_pid, title, lines)
        if action == "f6":
            return self._native_open_menu(path, client_pid)
        if action in {"panes_menu", "layout_menu"}:
            return self._native_open_menu(path, client_pid,
                                          section="panes" if action == "panes_menu" else "layout")
        if action in {"help", "show_logs", "show_panes"}:
            kind = "help" if action == "help" else "panes" if action == "show_panes" else "logs"
            _session, title, lines = self._native_overlay_content(path, client_pid, kind)
            return self._native_open_text_overlay(path, client_pid, title, lines)
        if (action == "mouse" and mouse_event == "down"
                and type(mouse_x) is int and type(mouse_y) is int):
            candidate = read_json(path, limit=2_000_000)
            if (candidate.get("session_record_path") == str(path)
                    and _layout_mode(candidate) == _NATIVE_CANVAS_LAYOUT
                    and mouse_pane == candidate.get("status_pane_id")):
                observed = self._verify(candidate)
                status = [pane for window in observed.get("windows", [])
                          for pane in window.get("panes", [])
                          if pane.get("pane_id") == mouse_pane]
                if (observed.get("state") == "ready" and len(status) == 1
                        and mouse_y == status[0].get("height", 0) - 1
                        and status[0].get("y", -1) + status[0].get("height", -1)
                        == observed.get("geometry", {}).get("rows")
                        and type(status[0].get("width")) is int):
                    _row, segments = _native_radio_row(
                        candidate, columns=status[0]["width"])
                    target = next(((event, value) for left, right, event, value in segments
                                   if left <= mouse_x < right), None)
                    if target is not None:
                        return self._native_control_event(path, client_pid, target[0],
                                                         operation_id=target[1])
                    return _reply("unchanged", effect="none", handle=candidate["handle"])
        directory = path.parent
        session = read_json(path, limit=2_000_000)
        if (session.get("session_record_path") != str(path)
                or _layout_mode(session) != _NATIVE_CANVAS_LAYOUT):
            raise ShellCommandError("native_control_identity_unknown", "The native control record does not match a canvas-v3 session.", effect="unknown")
        with session_lock(directory, blocking=True):
            session = read_json(path, limit=2_000_000)
            observed = self._verify(session)
            if session.get("state") != "ready" or observed.get("state") != "ready":
                raise ShellCommandError("native_control_identity_unknown", "The native control session is not ready.", effect="unknown")
            if not self._native_primary_client(directory, session, client_pid):
                raise ShellCommandError("native_control_primary_required", "Only the exact primary canvas client can control the layout.")
            operations = self._native_canvas_operations(directory, session)
            self._native_inventory(session, observed, operations, allow_active=True)
            state = _validate_native_layout(session.get("native_layout"))
            if action in {"next_pane", "previous_pane"}:
                plan = self._plan_native_operations(operations, state, session)
                order = [item["operation_id"] for item in plan["assignments"]]
                if not order:
                    return _reply("empty", handle=session["handle"])
                selected = state.get("selected_operation_id")
                index = order.index(selected) if selected in order else 0
                target = order[(index + (1 if action == "next_pane" else -1)) % len(order)]
                return self._begin_native_inspection_locked(directory, session, operations, target,
                                                            defer=True)
            summary_slots = (session.get("summary_panes") if isinstance(session.get("summary_panes"), list)
                             else [session])
            summary_index = next((index for index, slot in enumerate(summary_slots)
                                  if slot.get("summary_pane_id") == mouse_pane), None)
            if (action == "mouse" and mouse_event == "down"
                    and summary_index is not None and type(mouse_y) is int):
                observed = self._verify(session)
                summaries = [pane for window in observed.get("windows", [])
                             if window.get("window_id") == session.get("status_window_id")
                             for pane in window.get("panes", [])
                             if pane.get("pane_id") == mouse_pane]
                layout = session["native_layout"]
                if layout.get("grouped_sections"):
                    groups = layout.get("collapsed_groups", [])
                    operation_ids = (groups[summary_index].get("operation_ids", [])
                                     if summary_index < len(groups) else [])
                    sections = [{"operation_id": item} for item in operation_ids]
                else:
                    sections = layout.get("collapsed_sections", [])
                if (len(summaries) == 1 and 0 <= mouse_y < len(sections)
                        and mouse_y < summaries[0]["height"]):
                    return self._begin_native_inspection_locked(
                        directory, session, operations,
                        sections[mouse_y]["operation_id"], defer=True)
            if action == "mouse" and mouse_event == "down" and type(mouse_y) is int:
                observed = self._verify(session)
                clicked = [pane for window in observed.get("windows", [])
                           for pane in window.get("panes", [])
                           if pane.get("pane_id") == mouse_pane and pane.get("role") == "operation"
                           and pane.get("window_id") == session.get("status_window_id")]
                if len(clicked) == 1 and 0 <= mouse_y < clicked[0]["height"] - 1:
                    return self._begin_native_inspection_locked(
                        directory, session, operations, str(clicked[0]["operation_id"]),
                        defer=True)
            backend = self.backend
            if action == "f6":
                show = getattr(backend, "show_native_menu", None)
                if not callable(show):
                    raise ShellCommandError("native_control_unavailable", "The native operation menu is unavailable.")
                native_state = _validate_native_layout(session.get("native_layout"))
                entries = []
                for operation in operations:
                    op_id = str(operation["operation_id"])
                    radio = "(*)" if op_id == native_state.get("selected_operation_id") else "( )"
                    label = f"{radio} {op_id} [{operation.get('state', 'unknown')}]"
                    entries.append({"label": label.encode("ascii", "replace").decode("ascii"),
                                    "action": "select", "operation_id": op_id})
                entries.extend([
                    {"label": "Layout: Follow", "action": "mode", "operation_id": "follow"},
                    {"label": "Layout: Stable", "action": "mode", "operation_id": "stable"},
                    {"label": "Layout: Fixed", "action": "mode", "operation_id": "fixed"},
                ])
                selected = native_state.get("selected_operation_id")
                if selected is not None:
                    entries.extend([
                        {"label": "More space for selected", "action": "increase_ratio", "operation_id": selected},
                        {"label": "Less space for selected", "action": "decrease_ratio", "operation_id": selected},
                        {"label": "Expand selected", "action": "expand", "operation_id": selected},
                        {"label": "Restore selected", "action": "restore", "operation_id": selected},
                        {"label": "Logs for selected", "action": "show_logs"},
                    ])
                entries.append({"label": "Help", "action": "help"})
                return show(session, client_pid, str(native_state["mode"]), entries)
            if action == "help":
                show = getattr(backend, "show_native_overlay", None)
                if not callable(show):
                    raise ShellCommandError("native_control_unavailable", "Native help overlay is unavailable.")
                return show(session, client_pid, "Help", [
                    "F6 opens operation radio controls.",
                    "Arrows select; Enter applies; Escape returns to the prior view.",
                    "Use shell layout --handle with --mode and --ratio for exact values.",
                    "Use shell layout --handle --expand or --restore for pane size control.",
                ])
            if action == "show_logs":
                show = getattr(backend, "show_native_overlay", None)
                state = _validate_native_layout(session.get("native_layout"))
                selected = state.get("selected_operation_id")
                if selected is None:
                    return _reply("empty", handle=session["handle"])
                paths = self._paths(directory, selected)
                try:
                    with log_lock(paths["cursor"]):
                        with paths["log"].open("rb") as stream:
                            stream.seek(0, os.SEEK_END)
                            stream.seek(max(0, stream.tell() - 16_384))
                            raw = stream.read(16_384)
                    output, _skipped = _decode_tail_page(raw, 8192)
                except (OSError, ShellCommandError):
                    output = "Output is not currently available."
                lines = [line.encode("ascii", "replace").decode("ascii")
                         for line in output.splitlines()[-20:]]
                if callable(show):
                    return show(session, client_pid, f"Logs: {selected}", lines)
                return _reply("unavailable", ok=False, handle=session["handle"])
            if action in {"select", "mode", "ratio", "expand", "restore", "page"}:
                if action == "select":
                    return self._change_native_layout_locked(
                        directory, session, operations, selected_operation_id=operation_id,
                        defer=True)
                if action == "mode":
                    return self._change_native_layout_locked(
                        directory, session, operations, mode=operation_id, defer=True)
                if action == "expand":
                    return self._change_native_layout_locked(
                        directory, session, operations, expand_operation_id=operation_id,
                        defer=True)
                if action == "restore":
                    return self._change_native_layout_locked(
                        directory, session, operations, restore_operation_id=operation_id,
                        defer=True)
                if action == "page":
                    try:
                        page = int(operation_id or "")
                    except ValueError as exc:
                        raise ShellCommandError("invalid_native_page", "The native page action is malformed.") from exc
                    return self._change_native_layout_locked(directory, session, operations,
                                                             page=page, defer=True)
                if action == "ratio":
                    if operation_id is None or "=" not in operation_id:
                        raise ShellCommandError("invalid_native_ratios", "The native ratio action is malformed.")
                    target, value = operation_id.split("=", 1)
                    return self._change_native_layout_locked(
                        directory, session, operations, ratios={target: value}, defer=True)
            if action in {"increase_ratio", "decrease_ratio"}:
                state = _validate_native_layout(session.get("native_layout"))
                target = operation_id or state.get("selected_operation_id")
                if target is None:
                    raise ShellCommandError("native_operation_missing", "Select an operation before adjusting its ratio.")
                weights = dict(state.get("weights", {}))
                current = weights.get(target, 1)
                settings = _native_canvas_settings(_runtime_settings_for_service(session, self.source_root))
                step = max(1, round(current * settings.get('ratio_step_percent', 10) / 100))
                new = min(MAX_DERIVED_WEIGHT, current + step) if action == "increase_ratio" else max(1, current - step)
                return self._change_native_layout_locked(
                    directory, session, operations, ratios={target: new},
                    derived_ratio_adjustment=True, defer=True)
            if action == "page_step":
                if operation_id not in {"-1", "+1"}:
                    raise ShellCommandError("invalid_native_page", "The native page step is malformed.")
                state = _validate_native_layout(session.get("native_layout"))
                current_plan = self._plan_native_operations(operations, state, session)
                target_page = min(max(0, current_plan["page"] + int(operation_id)),
                                  current_plan["page_count"] - 1)
                if target_page == current_plan["page"]:
                    return _reply("unchanged", effect="none", handle=session["handle"])
                return self._change_native_layout_locked(
                    directory, session, operations,
                    mode="stable" if state["mode"] == "follow" else None,
                    page=target_page, defer=True,
                )
            if action == "mouse":
                action = "divider"
            if action in {"divider", "border", "escape"}:
                return self._native_divider_event(
                    directory, session, operations, client_pid, action=action,
                    mouse_x=mouse_x, mouse_y=mouse_y, mouse_event=mouse_event,
                    mouse_pane=mouse_pane, border=action == "border",
                )
            raise ShellCommandError("unsupported_native_control", "The native control action is unsupported.")

    def _native_divider_event(
        self, directory: Path, session: dict[str, Any], operations: Sequence[Mapping[str, Any]],
        client_pid: int, *, action: str, mouse_x: int | None, mouse_y: int | None,
        mouse_event: str | None, mouse_pane: str | None, border: bool = False,
    ) -> dict[str, Any]:
        state = _validate_native_layout(session.get("native_layout"))
        drag = state.get("divider_drag")
        if action == "escape":
            if not isinstance(drag, Mapping):
                return _reply("unchanged", effect="none", handle=session["handle"])
            state["weights"] = dict(drag.get("prior_weights", {}))
            state["mode"] = str(drag.get("prior_mode", "stable"))
            state["divider_drag"] = None
            state["desired_generation"] += 1
            state["pending"] = True
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return self._apply_native_layout_locked(directory, session)
        if (mouse_event not in {"down", "drag", "up"}
                or (not border and (type(mouse_x) is not int or type(mouse_y) is not int))
                or (border and (mouse_x is not None or mouse_y is not None))):
            raise ShellCommandError("invalid_native_divider", "The divider gesture coordinates are malformed.")
        native = self._verify(session)
        controls = native.get("controls", {})
        source_panes = [pane for window in native.get("windows", [])
                        for pane in window.get("panes", [])
                        if pane.get("pane_id") == mouse_pane]
        if (native.get("state") != "ready" or len(source_panes) != 1
                or type(source_panes[0].get("y")) is not int):
            raise ShellCommandError("native_divider_identity_unknown",
                                    "The pointer pane cannot be mapped to the exact native canvas.",
                                    effect="unknown")
        # tmux's mouse_x/mouse_y are pane-local, even for a pane at the bottom
        # of the window. Compare against the tagged layout's window positions.
        if border and mouse_event in {"drag", "up"}:
            if not isinstance(drag, Mapping) or drag.get("client_pid") != client_pid:
                raise ShellCommandError("native_divider_identity_unknown", "The divider drag owner changed.", effect="unknown")
            if mouse_event == "drag":
                return _reply("dragging", effect="known", handle=session["handle"])
            state["weights"] = dict(drag.get("prior_weights", {}))
            state["mode"] = str(drag.get("prior_mode", "stable"))
            state["divider_drag"] = None
            state["pending"] = state["desired_generation"] > state["applied_generation"]
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return _reply("unchanged", effect="known", handle=session["handle"])
        window_y = (source_panes[0]["y"] + mouse_y) if not border else None
        rects = controls.get("panes", []) if isinstance(controls, Mapping) else []
        ordered = sorted((item for item in rects if isinstance(item, Mapping)
                          and isinstance(item.get("operation_id"), str)
                          and type(item.get("y")) is int and type(item.get("height")) is int),
                         key=lambda item: (item["y"], item["operation_id"]))
        pair: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
        for first, second in zip(ordered, ordered[1:]):
            boundary = first["y"] + first["height"]
            if (first.get("pane_id") == mouse_pane if border
                    else abs(window_y - boundary) <= 1):
                pair = first, second
                if border:
                    window_y = boundary
                break
        if mouse_event == "down":
            if pair is None:
                raise ShellCommandError("native_divider_missing", "The pointer is not on a tagged operation divider.")
            if mouse_pane is not None and mouse_pane not in {
                    pair[0].get("pane_id"), pair[1].get("pane_id"),
                    pair[0].get("operation_id"), pair[1].get("operation_id")}:
                raise ShellCommandError("native_divider_identity_unknown", "The pointer pane does not match the adjacent tagged operations.", effect="unknown")
            state["divider_drag"] = {
                "started_monotonic": time.monotonic(), "client_pid": client_pid,
                "first": str(pair[0]["operation_id"]), "second": str(pair[1]["operation_id"]),
                "start_y": window_y,
                "first_rows": max(_NATIVE_MIN_PANE_ROWS, int(pair[0].get("rows", pair[0]["height"]))),
                "second_rows": max(_NATIVE_MIN_PANE_ROWS, int(pair[1].get("rows", pair[1]["height"]))),
                "prior_weights": dict(state.get("weights", {})), "prior_mode": state["mode"],
            }
            state["mode"] = "fixed"
            state["pending"] = True
            session["native_layout"] = state
            atomic_json(directory / "session.json", session)
            return _reply("dragging", effect="known", handle=session["handle"])
        if not isinstance(drag, Mapping) or drag.get("client_pid") != client_pid:
            raise ShellCommandError("native_divider_identity_unknown", "The divider drag owner changed.", effect="unknown")
        delta = window_y - int(drag["start_y"])
        first_rows = max(_NATIVE_MIN_PANE_ROWS, int(drag["first_rows"]) + delta)
        second_rows = max(_NATIVE_MIN_PANE_ROWS, int(drag["second_rows"]) - delta)
        first_weight, second_weight = max(1, first_rows - _NATIVE_MIN_PANE_ROWS), max(1, second_rows - _NATIVE_MIN_PANE_ROWS)
        state["weights"].update({str(drag["first"]): first_weight,
                                 str(drag["second"]): second_weight})
        state["mode"] = "fixed"
        state["desired_generation"] += 1
        if mouse_event == "up":
            state["divider_drag"] = None
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        if mouse_event == "drag":
            return _reply("dragging", effect="known", handle=session["handle"],
                          desired_generation=state["desired_generation"])
        return self._apply_native_layout_locked(directory, session)

    def run_native_control_loop(self, session_record: str | os.PathLike[str]) -> int:
        """Poll existing capture timestamps and apply the one native layout plan."""
        path = Path(session_record)
        if not path.is_absolute() or path.name != "session.json":
            return 2
        directory = path.parent
        last_control_row: str | None = None
        last_control_size: tuple[int, int] | None = None
        last_home: bool | None = None
        last_redraw_generation: int | None = None
        from .command_live_configuration import ConfigurationConsumer
        configuration = ConfigurationConsumer()
        while True:
            fast_poll = False
            poll_interval = .25
            try:
                session = read_json(path, limit=2_000_000)
                if (_layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                        or session.get("session_record_path") != str(path)
                        or session.get("state") in {"closed", "removed"}):
                    return 0
                if session.get("state") != "ready":
                    if session.get("state") == "retiring":
                        from .command_canvas import _clip
                        from .command_theme import ansi, palette
                        try:
                            width = os.get_terminal_size(sys.stdout.fileno()).columns
                        except OSError:
                            width = int((session.get("native_layout") or {}).get("columns", 120))
                        message = ("Session finished · cleanup pending · Exit or Ctrl-Q"
                                   if session.get("cleanup_state") in {"unknown", "pending"} else
                                   "Session finished · output remains available · Exit or Ctrl-Q")
                        if message != last_control_row:
                            colors = palette(session)
                            sys.stdout.write("\x1b[999;1H\r\x1b[2K" +
                                             ansi(colors["text"], colors["toolbar"]) +
                                             _clip(message, max(1, width - 1)) + "\x1b[0m")
                            sys.stdout.flush()
                            last_control_row = message
                        try:
                            with session_lock(directory):
                                current = read_json(path, limit=2_000_000)
                                if current.get("state") == "retiring":
                                    self._native_retirement_tick_locked(directory, current)
                        except ShellCommandError:
                            pass
                    time.sleep(.25)
                    continue
                configuration.poll(self, directory, session)
                # Yield to lifecycle and input operations. The helper retries
                # on its next 250 ms tick instead of holding a queued blocking
                # lock that can delay cancel or close indefinitely.
                with session_lock(directory):
                    session = read_json(path, limit=2_000_000)
                    if session.get("state") in {"closed", "removed"}:
                        return 0
                    if _layout_mode(session) != _NATIVE_CANVAS_LAYOUT:
                        return 0
                    from .command_viewport import reconcile_primary
                    if reconcile_primary(self, directory, session):
                        atomic_json(path, session)
                    state = _validate_native_layout(session.get("native_layout"))
                    operations = self._native_canvas_operations(directory, session)
                    try:
                        size = os.get_terminal_size(sys.stdout.fileno())
                        control_size = (size.columns, size.lines)
                    except OSError:
                        control_size = (state["columns"], state["rows"])
                    control_row, _segments = _native_radio_row(
                        session, columns=control_size[0])
                    from .command_theme import footer
                    control_row = footer(session, control_row, _segments)
                    show_home = (control_size[1] > 1 and not state.get("selected_operation_id")
                                 and not state.get("collapsed_sections"))
                    redraw_generation = session.get("ui_redraw_generation", 0)
                    if (control_row != last_control_row or control_size != last_control_size
                            or show_home != last_home or redraw_generation != last_redraw_generation):
                        # The tagged control pane owns this one-row radio
                        # strip. Workload output stays in its native panes and
                        # is never redrawn by this loop.
                        # The control pane fills the window before the first
                        # workload exists; position its radio strip at the
                        # physical bottom in both that state and the later
                        # one-row footer state.
                        if show_home:
                            from .command_canvas import native_home_lines
                            from .command_theme import ansi, palette
                            colors = palette(session)
                            lines = native_home_lines(session, columns=control_size[0], rows=control_size[1])
                            sys.stdout.write((ansi(colors["text"], colors["surface"])
                                              if session.get("runtime_settings", {}).get("colors_enabled", True)
                                              else "\x1b[0m")
                                             + "\x1b[2J\x1b[H" + "\r\n".join(lines))
                        elif control_size != last_control_size or last_home:
                            sys.stdout.write("\x1b[2J\x1b[H")
                        sys.stdout.write("\x1b[999;1H\r\x1b[2K" + control_row)
                        sys.stdout.flush()
                        last_control_row = control_row
                        last_control_size = control_size
                        last_home = show_home
                        last_redraw_generation = redraw_generation
                    now = time.monotonic()
                    if state.get("inspection_restore") is not None:
                        observed_controls = self._verify(session).get("controls", {})
                        if self._native_inspection_tick_locked(
                                directory, session, now=now, controls=observed_controls):
                            session = read_json(path, limit=2_000_000)
                            state = _validate_native_layout(session.get("native_layout"))
                    snapshots = dict(state.get("activity_snapshot", {}))
                    resume_events: list[tuple[float, str]] = []
                    quiet_seconds = float(_native_canvas_settings(
                        _runtime_settings_for_service(session, self.source_root)).get(
                            "quiet_seconds", _NATIVE_QUIET_SECONDS))
                    if state.get("grouped_sections"):
                        titles = self._native_title_states(operations, now=now,
                                                            quiet_seconds=quiet_seconds)
                        if titles != session.get("native_title_states"):
                            update_titles = getattr(self.backend, "update_native_title_states", None)
                            if callable(update_titles):
                                try:
                                    inspected = self._verify(session)
                                    inventory = self._native_inventory(
                                        session, inspected, operations, allow_active=True)
                                    update_titles(session, titles, inventory)
                                except ShellCommandError as title_error:
                                    session["native_title_error"] = title_error.code
                                else:
                                    session["native_title_states"] = titles
                                    session.pop("native_title_error", None)
                    for operation in operations:
                        op_id = str(operation["operation_id"])
                        activity = operation.get("activity_monotonic", operation.get("activated_monotonic", now))
                        if type(activity) not in {int, float}:
                            continue
                        previous = snapshots.get(op_id)
                        if (type(previous) in {int, float} and activity > previous
                                and now - float(previous) >= quiet_seconds):
                            resume_events.append((float(activity), op_id))
                        snapshots[op_id] = activity
                    state["activity_snapshot"] = snapshots
                    forced_id = session.pop("follow_event_operation_id", None)
                    forced_at = session.pop("follow_event_monotonic", None)
                    if isinstance(forced_id, str) and forced_id in {str(item["operation_id"]) for item in operations}:
                        resume_events.append((float(forced_at) if type(forced_at) in {int, float} else now, forced_id))
                    deferred = state.get("pending_follow_event")
                    if (isinstance(deferred, Mapping)
                            and isinstance(deferred.get("operation_id"), str)
                            and deferred["operation_id"] in {str(item["operation_id"]) for item in operations}
                            and type(deferred.get("at")) in {int, float}):
                        resume_events.append((float(deferred["at"]), deferred["operation_id"]))
                    if state["mode"] == "follow" and resume_events:
                        state["operation_order"] = reactivation_order(state["operation_order"], resume_events)
                        event_at, event_id = max(resume_events)
                        last_auto = state.get("last_automatic_change_monotonic", 0.0)
                        automatic_min = float(_native_canvas_settings(
                            _runtime_settings_for_service(session, self.source_root)).get(
                                "automatic_view_min_seconds", 2))
                        first_automatic_placement = not state.get("last_automatic_apply_monotonic")
                        if (event_id != state.get("selected_operation_id")
                                and (first_automatic_placement or not last_auto
                                     or now - float(last_auto) >= automatic_min)):
                            state["selected_operation_id"] = event_id
                            state["manual_page"] = False
                            state["last_automatic_change_monotonic"] = now
                            state["desired_generation"] += 1
                            state["pending"] = True
                            if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
                                state["layout_state"] = "desired"
                            state["pending_follow_event"] = None
                        elif event_id != state.get("selected_operation_id"):
                            state["pending_follow_event"] = {
                                "operation_id": event_id, "at": event_at}
                        else:
                            state["pending_follow_event"] = None
                    elif state["mode"] != "follow":
                        state["pending_follow_event"] = None
                    drag = state.get("divider_drag")
                    if isinstance(drag, Mapping) and now - float(drag.get("started_monotonic", 0)) > 5.0:
                        state["weights"] = dict(drag.get("prior_weights", {}))
                        state["mode"] = str(drag.get("prior_mode", "stable"))
                        state["divider_drag"] = None
                        state["desired_generation"] += 1
                        state["pending"] = True
                    session["native_layout"] = state
                    atomic_json(path, session)
                    runtime = _runtime_settings_for_service(session, self.source_root)
                    native_settings = _native_canvas_settings(runtime)
                    poll_interval = int(native_settings.get("activity_coalesce_ms", 250)) / 1000
                    resize_wait = int(native_settings.get(
                        "reflow_debounce_ms" if state.get("grouped_sections") else "resize_debounce_ms",
                        100 if state.get("grouped_sections") else 250)) / 1000
                    automatic_floor = float(native_settings.get("automatic_view_min_seconds", 2))
                    last_resize = state.get("last_resize_monotonic", 0.0)
                    last_activation = state.get("last_activation_monotonic", 0.0)
                    last_applied = state.get("last_automatic_apply_monotonic", 0.0)
                    recent_resize = (type(last_resize) in {int, float} and last_resize
                                     and now - float(last_resize) < resize_wait)
                    recent_launch = (type(last_activation) in {int, float} and last_activation
                                     and now - float(last_activation) < resize_wait)
                    _items, prospective_plan = self._native_layout_plan(directory, session, now=now)
                    prospective_signature = _digest({
                        key: value for key, value in prospective_plan.items()
                        if key != "generation"})
                    wants_change = (state["desired_generation"] > state["applied_generation"]
                                    or state.get("applied_plan_signature") != prospective_signature)
                    fast_poll = ((wants_change or state.get("pending_follow_event") is not None)
                                 and state.get("layout_state") not in {"applying", "dispatching", "unknown"})
                    resize_overrides_floor = (type(last_resize) in {int, float}
                                              and type(last_applied) in {int, float}
                                              and float(last_resize) > float(last_applied))
                    manual_pending = state.get("manual_change_pending") is True
                    manual_due = state.get("manual_due_monotonic")
                    manual_wait = (manual_pending and type(manual_due) in {int, float}
                                   and now < float(manual_due))
                    floor_active = (wants_change and not manual_pending and not resize_overrides_floor
                                    and type(last_applied) in {int, float} and last_applied
                                    and now - float(last_applied) < automatic_floor)
                    unresolved = state.get("layout_state") in {"applying", "dispatching", "unknown"}
                    if not unresolved and (recent_resize or (recent_launch and not manual_pending)
                                           or floor_active or manual_wait):
                        state.update(pending=True, activity_badge=True,
                                     layout_state=(state.get("layout_state")
                                                   if state.get("layout_state") in
                                                   {"applying", "dispatching", "unknown"}
                                                   else "resize_pending" if recent_resize
                                                   else "desired"))
                        session["native_layout"] = state
                        session["native_control_badge"] = (
                            "Resize pending" if recent_resize else "Activity pending")
                        atomic_json(path, session)
                    elif wants_change or unresolved:
                        outcome = self._apply_native_layout_locked(directory, session)
                        if outcome.get("state") == "applied":
                            applied_session = read_json(path, limit=2_000_000)
                            applied_state = _validate_native_layout(applied_session.get("native_layout"))
                            if applied_state.get("manual_change_pending"):
                                applied_state["manual_change_pending"] = False
                                applied_state["manual_due_monotonic"] = None
                                applied_session["native_layout"] = applied_state
                                atomic_json(path, applied_session)
                        if (outcome.get("state") == "applied"
                                and not outcome.get("unchanged")
                                and any(item.get("visible") for item in prospective_plan["assignments"])):
                            applied_session = read_json(path, limit=2_000_000)
                            applied_state = _validate_native_layout(applied_session.get("native_layout"))
                            applied_state["last_automatic_apply_monotonic"] = time.monotonic()
                            applied_session["native_layout"] = applied_state
                            atomic_json(path, applied_session)
                    current = read_json(path, limit=2_000_000)
                    if current.get("state") == "ready" and isinstance(current.get("owner"), Mapping):
                        self._native_retirement_tick_locked(directory, current)
            except ShellCommandError as exc:
                if exc.code == "state_unavailable" and not path.exists():
                    return 0
                if exc.code == "busy" and exc.effect == "none":
                    # An input action briefly owns the record lock. Recheck
                    # the new record promptly instead of stacking this error
                    # sleep with the ordinary idle poll interval.
                    time.sleep(.02)
                    continue
                if exc.effect == "unknown":
                    try:
                        with session_lock(directory, blocking=True):
                            session = read_json(path, limit=2_000_000)
                            state = _validate_native_layout(session.get("native_layout"))
                            state.update(layout_state="unknown", layout_error=exc.code, pending=True)
                            session["native_layout"] = state
                            atomic_json(path, session)
                    except (OSError, ShellCommandError):
                        return 2
                    time.sleep(.25)
                else:
                    time.sleep(.25)
            except (OSError, ValueError, TypeError, KeyError):
                time.sleep(.25)
            if fast_poll:
                # A pending resize or launch is bounded by its 250 ms debounce,
                # not by another full 250 ms polling interval.
                time.sleep(.02)
                continue
            try:
                observed_mtime = path.stat().st_mtime_ns
            except OSError:
                time.sleep(.05)
                continue
            # Idle maintenance uses the configured coalescing cadence. A viewport or
            # control request updates the session record and wakes it sooner.
            deadline = time.monotonic() + poll_interval
            while time.monotonic() < deadline:
                time.sleep(min(.05, deadline - time.monotonic()))
                try:
                    if path.stat().st_mtime_ns != observed_mtime:
                        break
                except OSError:
                    break

    def resolve_alias(self, alias: str) -> str:
        """Resolve a display alias only when exactly one private session owns it."""
        if not re.fullmatch(r"ls-[a-f0-9]{8}-[a-f0-9]{8}", alias):
            raise ShellCommandError("invalid_session_alias", "The shell session alias is malformed.")
        matches: list[str] = []
        for path in self._sessions(create=False).glob("*/*/session.json"):
            record = read_json(path, limit=2_000_000)
            if record.get("native_name") == alias:
                handle = record.get("handle")
                if not isinstance(handle, str):
                    raise ShellCommandError("session_alias_ambiguous", "The alias has an incomplete owner record.")
                matches.append(handle)
        if len(matches) != 1:
            raise ShellCommandError("session_alias_missing" if not matches else "session_alias_ambiguous",
                                    "The session alias does not identify one exact owned session.")
        return matches[0]

    def ensure(
        self, *, cwd: str | os.PathLike[str], settings: Mapping[str, Any],
        settings_digest: str, owner: Mapping[str, Any] | None = None,
        profile_name: str = "agent-shell", profile_base: str | None = None,
        layout_mode: str | None = None,
        replace_retiring: bool = False,
        runtime_settings: Mapping[str, Any] | None = None,
        runtime_warnings: Sequence[Mapping[str, str]] = (),
    ) -> dict[str, Any]:
        layout_was_explicit = layout_mode is not None
        if layout_mode is None:
            layout_mode = _DEFAULT_LAYOUT_MODE
        capability = self.backend.capability()
        if capability.get("state") != "available":
            return _reply("unavailable", ok=False, backend=capability)
        resolved = _settings(settings, source_root=self.source_root, profile_name=profile_name,
                             profile_base=profile_base)
        if not isinstance(settings_digest, str) or not _HEX64.fullmatch(settings_digest):
            raise ShellCommandError("invalid_profile_digest", "A validated profile digest is required.")
        if _profile_digest(resolved, profile_name) != settings_digest:
            raise ShellCommandError("profile_digest_mismatch", "The profile digest does not match the resolved settings.")
        if layout_mode not in {*_FAMILY_MODES, _LEGACY_LAYOUT}:
            raise ShellCommandError("invalid_layout_mode", "Shell layout must be family, legacy, canvas-v2, or canvas-v3.")
        if replace_retiring and layout_mode not in _FAMILY_MODES:
            raise ShellCommandError("invalid_rollover", "Only a new family session may replace a retiring owned view.")
        runtime_defaults = load_tmux_runtime_defaults(
            source_root=self.source_root, name=profile_base or "agent-shell")
        runtime = dict(runtime_settings) if runtime_settings is not None else runtime_defaults["settings"]
        runtime_schema = self.source_root / "ls/config/application-profile-tmux-runtime.schema.json"
        if validate_json_schema(runtime_defaults | {"settings": runtime}, runtime_schema,
                                label="tmux runtime settings"):
            raise ShellCommandError("invalid_runtime_settings", "The tmux runtime baseline failed its schema.")
        runtime_digest = _digest(runtime)
        try:
            workdir = Path(cwd).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ShellCommandError("invalid_cwd", "The shell working directory is unavailable.") from exc
        if not workdir.is_dir():
            raise ShellCommandError("invalid_cwd", "The shell working directory must be a directory.")
        binding = _owner(owner)
        scope = _scope(binding)
        self._prepare_root()
        if (layout_mode in _FAMILY_MODES or not layout_was_explicit) and binding.get("agent_id"):
            bound_families: list[Path] = []
            stale_membership = False
            for path in self._sessions(create=False).glob("*/*/session.json"):
                candidate = read_json(path, limit=2_000_000)
                if _layout_mode(candidate) not in _FAMILY_MODES or candidate.get("state") != "ready":
                    continue
                members = candidate.get("members", {})
                if not isinstance(members, dict):
                    raise ShellCommandError("family_members_unknown", "A family membership record is malformed.", effect="unknown")
                for member in members.values():
                    if isinstance(member, dict) and member.get("agent_id") == binding["agent_id"]:
                        if member.get("owner_fingerprint") == owner_fingerprint(binding):
                            bound_families.append(path)
                        else:
                            stale_membership = True
            if len(bound_families) > 1:
                raise ShellCommandError("family_membership_ambiguous", "This agent belongs to multiple family sessions.", effect="unknown")
            if bound_families:
                path = bound_families[0]
                directory = path.parent
                with session_lock(directory):
                    family = read_json(path, limit=2_000_000)
                    self._family_caller(family, binding)
                    if layout_was_explicit and _layout_mode(family) != layout_mode:
                        raise ShellCommandError("layout_mode_conflict", "The existing family session keeps its original layout.")
                    if (owner_liveness(family.get("owner", {})) != "live"
                            or family.get("settings_digest") != settings_digest
                            or self._verify(family).get("state") != "ready"):
                        raise ShellCommandError("family_reconcile_required", "The joined family cannot be reused safely.", effect="unknown")
                    return self._session_reply(family, created=False, joined=True) | {"role": "member"}
            if stale_membership:
                raise ShellCommandError("member_recovery_required", "An earlier family membership needs exact recovery.")
        owner_dir = self._owner_dir(scope)
        with session_lock(owner_dir):
            owner_path = owner_dir / "owner.json"
            previous_cleanup: dict[str, Any] | None = None
            if owner_path.exists():
                prior = read_json(owner_path)
                old = prior.get("owner")
                if not isinstance(old, dict):
                    raise ShellCommandError("owner_identity_unknown", "The previous shell owner binding is incomplete.", effect="unknown")
                changed_owner = owner_fingerprint(old) != owner_fingerprint(binding)
                if changed_owner:
                    life = owner_liveness(old)
                    try:
                        same_scope = _scope(old) == scope
                    except (KeyError, TypeError):
                        same_scope = False
                    if life != "dead" or not same_scope:
                        raise ShellCommandError(
                            "recovery_required" if life == "dead" else "owner_identity_unknown",
                            "An earlier owner binding exists; explicit recovery is required before takeover.",
                            effect="unknown" if life == "unknown" else "none",
                        )
                old_handle = prior.get("active_handle")
                if not isinstance(old_handle, str):
                    raise ShellCommandError("owner_identity_unknown", "The prior active session handle is missing.", effect="unknown")
                try:
                    directory, record, old_op_id = self._load(old_handle)
                except ShellCommandError as exc:
                    raise ShellCommandError("owner_identity_unknown", "The prior session cannot be resolved.", effect="unknown") from exc
                if (old_op_id is not None or record.get("scope_key") != scope
                        or record.get("owner_fingerprint") != owner_fingerprint(old)
                        or record.get("handle") != old_handle):
                    raise ShellCommandError("owner_identity_unknown", "The prior session identity is inconsistent.", effect="unknown")
                if (record.get("state") not in {"closed", "removed"}
                        and not (replace_retiring and record.get("state") == "retiring"
                                 and _layout_mode(record) in {_LEGACY_LAYOUT, *_FAMILY_MODES})):
                    if changed_owner:
                        raise ShellCommandError("recovery_required", "The previous live session requires explicit adoption.")
                    else:
                        self._authorize(record, binding)
                        if layout_was_explicit and _layout_mode(record) != layout_mode:
                            raise ShellCommandError("layout_mode_conflict", "The existing shell session keeps its original layout; close it before creating a different layout.")
                        if record.get("settings_digest") != settings_digest:
                            raise ShellCommandError("profile_conflict", "The active session profile is frozen; apply it explicitly.")
                        if record.get("state") != "ready":
                            raise ShellCommandError("session_reconcile_required", "The owned session has uncertain lifecycle state.", effect="unknown")
                        if self._verify(record).get("state") != "ready":
                            raise ShellCommandError("session_identity_unknown", "The exact owned tmux session could not be verified.", effect="unknown")
                        if _family_like(record):
                            effective = (_canvas_effective_settings(resolved, runtime,
                                                                   native=bool((record.get("native_layout") or {}).get("grouped_sections")))
                                         if _layout_mode(record) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else
                                         _family_effective_settings(resolved, runtime))
                            old_runtime = _runtime_settings_for_service(record, self.source_root)
                            if (runtime["layout"]["canvas_columns"] != old_runtime["layout"]["canvas_columns"]
                                    or effective["history_limit"] != record["settings"]["history_limit"]):
                                raise ShellCommandError(
                                    "family_geometry_requires_new_session",
                                    "Existing family panes keep their original width and history capacity; retire this session before creating the new canvas.",
                                )
                        with session_lock(directory):
                            resetter = getattr(self.backend, "apply_runtime_baseline", None)
                            if callable(resetter):
                                native = self._verify(record)
                                self._native_inventory(record, native, self._strict_ops(directory, record),
                                                       allow_active=True)
                                resetter(record | {"runtime_settings": runtime})
                            record.update(runtime_settings=runtime, runtime_digest=runtime_digest,
                                          runtime_warnings=list(runtime_warnings))
                            record["last_activity_at"] = self.clock()
                            atomic_json(directory / "session.json", record)
                        return self._session_reply(record, created=False, joined=True)
                with session_lock(directory):
                    record = read_json(directory / "session.json")
                    if (record.get("handle") != old_handle or record.get("scope_key") != scope
                            or record.get("owner_fingerprint") != owner_fingerprint(old)):
                        raise ShellCommandError("owner_identity_unknown", "The prior session changed identity.", effect="unknown")
                    operations = self._strict_ops(directory, record)
                    pins, pin_unknown = self._pins(directory)
                    observed = self._verify(record)
                    if (replace_retiring and _layout_mode(record) in {_LEGACY_LAYOUT, *_FAMILY_MODES}
                            and record.get("state") == "retiring"
                            and type(record.get("retirement_armed_at")) in {int, float}
                            and self._terminal_ops_collected(operations)
                            and not pin_unknown and observed.get("state") == "ready"
                            and type(observed.get("session", {}).get("attached")) is int
                            and observed["session"]["attached"] > 0
                            and len(pins) <= observed["session"]["attached"]):
                        self._native_inventory(record, observed, operations)
                        previous_cleanup = {"handle": old_handle, "state": "retiring",
                                            "cleanup_state": "waiting_viewers",
                                            "at": record["retirement_armed_at"]}
                        continue_rollover = True
                    else:
                        continue_rollover = False
                    cleanup_at = (record.get("gc_at") if type(record.get("gc_at")) in {int, float}
                                  else record.get("closed_at"))
                    if (not continue_rollover and (record.get("state") not in {"closed", "removed"}
                            or record.get("cleanup_state") != "removed"
                            or not self._terminal_ops_collected(operations)
                            or pins or pin_unknown or observed.get("state") != "missing"
                            or not self._endpoint_absent(record)
                            or type(cleanup_at) not in {int, float})):
                        raise ShellCommandError("previous_cleanup_unknown", "The prior session cleanup cannot be proven.", effect="unknown")
                    if not continue_rollover:
                        previous_cleanup = {
                            "handle": old_handle, "state": record["state"],
                            "cleanup_state": "removed", "at": cleanup_at,
                            "source": "gc" if type(record.get("gc_at")) in {int, float} else "close",
                        }

            # An omitted layout reuses the recorded session above, including
            # legacy and family-v1 sessions. Qualify the new default only when
            # a new native canvas is actually going to be created.
            if layout_mode in _FAMILY_MODES and capability.get("version") != "tmux 3.4":
                raise ShellCommandError(
                    "family_tmux_unqualified" if layout_mode != _NATIVE_CANVAS_LAYOUT else "native_canvas_unqualified",
                    "The requested native layout is qualified only for tmux 3.4.",
                )
            if layout_mode == _NATIVE_CANVAS_LAYOUT and any(
                    not callable(getattr(self.backend, name, None))
                    for name in ("inspect_canvas_v3", "apply_native_layout", "create_native_operation")):
                raise ShellCommandError("native_canvas_unqualified",
                                        "The native canvas backend is not fully qualified.")
            sid = uuid.uuid4().hex
            handle = self._handle(scope, sid)
            alias = f"ls-{scope[:8]}-{sid[:8]}"
            try:
                self.resolve_alias(alias)
            except ShellCommandError as exc:
                if exc.code != "session_alias_missing":
                    raise
            else:
                raise ShellCommandError("session_alias_collision", "The generated short alias is already in use.")
            record: dict[str, Any] = {
                "schema_version": 1, "handle": handle, "scope_key": scope, "session_id": sid,
                "session_key": uuid.uuid4().hex, "native_name": alias,
                "endpoint": str(self.backend.socket_path(sid)),
                "machine_generation": binding["machine_generation"], "host": binding["host"],
                "uid": binding["uid"], "owner": binding, "owner_fingerprint": owner_fingerprint(binding),
                "settings": (_family_effective_settings(resolved, runtime) if layout_mode == _FAMILY_LAYOUT else
                             _canvas_effective_settings(resolved, runtime,
                                                        native=layout_mode == _NATIVE_CANVAS_LAYOUT)
                             if layout_mode in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else resolved),
                "requested_settings": resolved, "settings_digest": settings_digest,
                "runtime_settings": runtime, "runtime_digest": runtime_digest,
                "qualified_tmux_version": capability.get("version") if layout_mode in _FAMILY_MODES else None,
                "runtime_warnings": list(runtime_warnings),
                "profile_name": profile_name, "profile_base": profile_base,
                "layout_mode": layout_mode,
                "canvas_width": runtime["layout"]["canvas_columns"]
                if layout_mode in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else None,
                "primary_viewport_height": (min(_FAMILY_DEFAULT_ROWS,
                                                _family_max_rows(_family_effective_settings(resolved, runtime)))
                                            if layout_mode == _FAMILY_LAYOUT else
                                            int(_native_canvas_settings(runtime).get("default_rows", _FAMILY_DEFAULT_ROWS))
                                            if layout_mode == _NATIVE_CANVAS_LAYOUT else _FAMILY_DEFAULT_ROWS),
                "members": {}, "member_invites": {},
                "state": "creating", "status": "creating", "created_at": self.clock(),
                "created_monotonic": time.monotonic(),
                "last_activity_at": self.clock(), "last_activity_kind": "ensure",
                "previous_cleanup": previous_cleanup,
            }
            if layout_mode == _NATIVE_CANVAS_LAYOUT:
                record["native_layout"] = _fresh_native_layout(runtime)
                record["native_history_retention"] = {
                    "state": "not_observed", "source": "native pane history and bounded durable logs",
                    "limit_bytes": record["settings"]["log_max_bytes"],
                }
            directory = self._session_dir(scope, sid, create=True)
            record["session_record_path"] = str(directory / "session.json")
            (directory / "operations").mkdir(mode=0o700)
            (directory / "pins").mkdir(mode=0o700)
            atomic_json(directory / "session.json", record, exclusive=True)
            atomic_json(owner_path, {"schema_version": 1, "scope_key": scope, "owner": binding,
                                     "owner_fingerprint": owner_fingerprint(binding),
                                     "active_handle": handle, "updated_at": self.clock()})
            try:
                native = self.backend.create_session(record, cwd=str(workdir), settings=record["settings"])
                record.update(native)
                record.update(state="ready", status="ready", effect_certainty="known",
                              last_activity_at=self.clock())
                atomic_json(directory / "session.json", record)
            except ShellCommandError as exc:
                record.update(state="unknown", status="unknown",
                              effect_certainty="unknown" if exc.effect == "unknown" else "known",
                              create_error=exc.code)
                atomic_json(directory / "session.json", record)
                raise ShellCommandError(exc.code, exc.message, effect=record["effect_certainty"]) from exc
            return self._session_reply(record, created=True, joined=False)

    def admit_member(
        self, handle: str, *, agent_id: str, label_slug: str,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a one-time root capability for one named native agent."""
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Member admission requires a family session handle.")
        self._authorize(session, owner)
        if not _family_like(session):
            raise ShellCommandError("family_layout_required", "Member admission requires a family-layout session.")
        agent_id = _trusted_agent_id(agent_id)
        label_slug = _trusted_member_slug(label_slug)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            self._authorize(session, owner)
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                raise ShellCommandError("session_not_ready", "The exact family session is not ready.", effect="unknown")
            members = session.get("members", {})
            if not isinstance(members, dict):
                raise ShellCommandError("family_members_unknown", "The family membership record is malformed.", effect="unknown")
            if any(member.get("agent_id") == agent_id for member in members.values()
                   if isinstance(member, dict)):
                raise ShellCommandError("member_already_admitted", "This native agent already has family membership.")
            invites = session.get("member_invites", {})
            if not isinstance(invites, dict):
                raise ShellCommandError("family_invites_unknown", "The family invitation record is malformed.", effect="unknown")
            for digest, invite in list(invites.items()):
                if isinstance(invite, dict) and invite.get("agent_id") == agent_id:
                    invites.pop(digest, None)
            token = secrets.token_urlsafe(32)
            token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            invites[token_digest] = {
                "agent_id": agent_id, "label_slug": label_slug,
                "issued_at": self.clock(),
            }
            session["member_invites"] = invites
            session["last_activity_at"] = self.clock()
            session["last_activity_kind"] = "member_admission"
            atomic_json(directory / "session.json", session)
        return _reply("invitation_issued", effect="known", handle=handle,
                      invitation=token, agent_id=agent_id, label_slug=label_slug,
                      join_command=self._shell_command("join", "--handle", handle) + " --invite <invitation>")

    def join_family(
        self, handle: str, *, invitation: str,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Claim a root-issued invitation and bind it to this live owner identity."""
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Joining requires a family session handle.")
        if not _family_like(session):
            raise ShellCommandError("family_layout_required", "Member admission requires a family-layout session.")
        caller = _owner(owner)
        token = invitation.encode("ascii", "strict") if isinstance(invitation, str) else b""
        if not token or len(token) > 512 or any(byte < 0x21 or byte > 0x7e for byte in token):
            raise ShellCommandError("invalid_invitation", "The family invitation is malformed.")
        token_digest = hashlib.sha256(token).hexdigest()
        with session_lock(directory):
            session = read_json(directory / "session.json")
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                raise ShellCommandError("session_not_ready", "The exact family session is not ready.", effect="unknown")
            if owner_liveness(session.get("owner", {})) != "live":
                raise ShellCommandError("family_root_not_live", "The family root must remain live while members join.")
            invites = session.get("member_invites", {})
            invite = invites.get(token_digest) if isinstance(invites, dict) else None
            if not isinstance(invite, dict):
                raise ShellCommandError("invalid_invitation", "The family invitation is unknown, expired, or already used.")
            agent_id = _trusted_agent_id(invite.get("agent_id"))
            label_slug = _trusted_member_slug(invite.get("label_slug"))
            if caller.get("agent_id") != agent_id:
                raise ShellCommandError("invitation_agent_mismatch", "The invitation was issued to a different native agent identity.")
            if (caller.get("machine_generation") != session.get("machine_generation")
                    or caller.get("host") != session.get("host") or caller.get("uid") != session.get("uid")):
                raise ShellCommandError("invitation_machine_mismatch", "The invited member must run on the family machine and account.")
            members = session.get("members", {})
            if not isinstance(members, dict):
                raise ShellCommandError("family_members_unknown", "The family membership record is malformed.", effect="unknown")
            if any(member.get("agent_id") == agent_id for member in members.values()
                   if isinstance(member, dict)):
                raise ShellCommandError("member_already_admitted", "This native agent already has family membership.")
            fingerprint = owner_fingerprint(caller)
            members[fingerprint] = {
                "agent_id": agent_id, "label_slug": label_slug,
                "owner": caller, "owner_fingerprint": fingerprint,
                "joined_at": self.clock(),
            }
            invites.pop(token_digest, None)
            session["members"] = members
            session["member_invites"] = invites
            session["last_activity_at"] = self.clock()
            session["last_activity_kind"] = "member_join"
            atomic_json(directory / "session.json", session)
        return _reply("joined", effect="known", handle=handle,
                      role="member", member_label=f"subagent {label_slug}",
                      join_command=self._shell_command("attach", "--handle", handle))

    def _ops(self, directory: Path) -> list[tuple[Path, dict[str, Any]]]:
        result = []
        try:
            paths = sorted((directory / "operations").glob("*.json"))
        except OSError:
            return result
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                operation = read_json(path, limit=2_000_000)
                if (isinstance(operation.get("operation_id"), str)
                        and isinstance(operation.get("operation_handle"), str)
                        and isinstance(operation.get("request_digest"), str)
                        and isinstance(operation.get("session_handle"), str)):
                    result.append((path, operation))
            except ShellCommandError:
                continue
        return result

    def _strict_ops(self, directory: Path, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Enumerate every operation receipt before any destructive lifecycle step."""
        operation_dir = directory / "operations"
        sidecars = (".payload.json", ".release.json", ".cursor.json",
                    ".drained.json", ".finalize.json")
        try:
            records = [path for path in operation_dir.iterdir()
                       if path.name.endswith(".json") and not path.name.endswith(sidecars)]
        except OSError as exc:
            raise ShellCommandError("operation_records_unknown", "Operation records cannot be enumerated safely.", effect="unknown") from exc
        operations: list[dict[str, Any]] = []
        for path in records:
            if path.is_symlink() or not path.is_file():
                raise ShellCommandError("operation_records_unknown", "An operation record is not a regular file.", effect="unknown")
            op = read_json(path, limit=2_000_000)
            op_id = op.get("operation_id")
            if (not isinstance(op_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", op_id)
                    or not _OP_HANDLE.fullmatch(str(op.get("operation_handle", "")))
                    or self._paths(directory, op_id)["record"] != path
                    or op.get("operation_handle") != self._op_handle(
                        str(session["scope_key"]), str(session["session_id"]), op_id)
                    or op.get("session_handle") != session.get("handle")
                    or op.get("session_key") != session.get("session_key")
                    or not isinstance(op.get("request_digest"), str)
                    or not _HEX64.fullmatch(op["request_digest"])):
                raise ShellCommandError("operation_records_unknown", "An operation record has uncertain identity.", effect="unknown")
            operations.append(op)
        return operations

    @staticmethod
    def _terminal_ops_collected(operations: Sequence[Mapping[str, Any]]) -> bool:
        return all(op.get("state") in {"completed", "failed", "collected"}
                   and op.get("process_scope") == "collected"
                   and op.get("output_drain") in {"available", "unavailable"}
                   for op in operations)

    @staticmethod
    def _endpoint_absent(session: Mapping[str, Any]) -> bool:
        try:
            Path(str(session["endpoint"])).lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _native_inventory(
        self, session: Mapping[str, Any], observed: Mapping[str, Any],
        operations: Sequence[Mapping[str, Any]], *, allow_active: bool = False,
        pending_operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Reject unknown panes before adoption or deletion of a private server."""
        if observed.get("state") != "ready":
            raise ShellCommandError("native_inventory_unknown", "The private session inventory is unavailable.", effect="unknown")
        windows = observed.get("windows", [])
        if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
            inventory = observed.get("inventory")
            inventory_windows = (inventory.get("windows")
                                 if isinstance(inventory, Mapping) else None)
            if not isinstance(inventory_windows, list) or not inventory_windows:
                raise ShellCommandError("native_inventory_unknown", "The native canvas pane inventory is unavailable.", effect="unknown")
            status_pane_id = session.get("status_pane_id") or session.get("control_pane_id")
            expected_ids: set[str] = {str(status_pane_id)} if isinstance(status_pane_id, str) else set()
            summary_panes = session.get("summary_panes")
            if isinstance(summary_panes, list):
                expected_ids.update(item["summary_pane_id"] for item in summary_panes
                                    if isinstance(item, Mapping) and isinstance(item.get("summary_pane_id"), str))
            elif isinstance(session.get("summary_pane_id"), str):
                expected_ids.add(session["summary_pane_id"])
            flattened: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
            for window in inventory_windows:
                panes = window.get("panes") if isinstance(window, Mapping) else None
                if not isinstance(panes, list):
                    raise ShellCommandError("native_inventory_unknown", "A native canvas window inventory is malformed.", effect="unknown")
                for pane in panes:
                    if not isinstance(pane, Mapping) or not isinstance(pane.get("pane_id"), str):
                        raise ShellCommandError("native_inventory_unknown", "A native canvas pane identity is malformed.", effect="unknown")
                    flattened.append((window, pane))
            status_panes = [(window, pane) for window, pane in flattened
                            if pane.get("role") == "status"]
            if len(status_panes) != 1 or (status_pane_id is not None
                    and status_panes[0][1].get("pane_id") != status_pane_id):
                raise ShellCommandError("native_inventory_unknown", "The exact native control pane changed identity.", effect="unknown")
            for operation in operations:
                state = operation.get("state")
                if state in {"completed", "failed", "collected"}:
                    if not self._terminal_ops_collected((operation,)):
                        raise ShellCommandError("operation_records_unknown", "A native canvas result lacks a drain receipt.", effect="unknown")
                elif state in {"intent", "running", "draining"} and (allow_active or operation.get("operation_id") == pending_operation_id):
                    if state != "intent" and operation.get("effect_certainty") != "known":
                        raise ShellCommandError("operation_records_unknown", "The native canvas operation is uncertain.", effect="unknown")
                else:
                    raise ShellCommandError("operation_records_unknown", "An uncertain native canvas operation blocks lifecycle change.", effect="unknown")
                native = operation.get("native_canvas")
                if not isinstance(native, Mapping):
                    if operation.get("operation_id") == pending_operation_id and state == "intent":
                        continue
                    raise ShellCommandError("operation_records_unknown", "A native operation lacks its pane identity pair.", effect="unknown")
                task_pane = str(native.get("pane_id", ""))
                parking_pane = str(native.get("parking_pane_id", ""))
                if not task_pane.startswith("%") or not parking_pane.startswith("%") or task_pane == parking_pane:
                    raise ShellCommandError("operation_records_unknown", "A native operation pane pair is malformed.", effect="unknown")
                pair_seen = [pane for _window, pane in flattened
                             if pane.get("pane_id") in {task_pane, parking_pane}]
                if operation.get("cleanup_state") == "removed":
                    if pair_seen:
                        raise ShellCommandError("native_inventory_unknown", "A removed native operation pane pair is still present.", effect="unknown")
                    continue
                if operation.get("cleanup_state") in {"dispatching", "unknown"}:
                    if not pair_seen:
                        # A prior removal may have succeeded before its local
                        # receipt was written. Reconciliation can prove absence
                        # without replaying the native mutation.
                        continue
                    if len(pair_seen) != 2:
                        raise ShellCommandError("native_inventory_unknown", "Only part of an uncertain native pane pair remains.", effect="unknown")
                matches = [(window, pane) for window, pane in flattened
                           if pane.get("pane_id") == task_pane
                           and pane.get("role") == "operation"
                           and pane.get("operation_id") == operation.get("operation_id")
                           and pane.get("request") == operation.get("request_digest")]
                placeholders = [(window, pane) for window, pane in flattened
                                if pane.get("pane_id") == parking_pane
                                and pane.get("role") in {"parking", "placeholder"}
                                and pane.get("operation_id") == operation.get("operation_id")
                                and pane.get("request") == operation.get("request_digest")]
                if len(matches) != 1 or len(placeholders) != 1:
                    raise ShellCommandError("native_inventory_unknown", "A native operation pane or parking placeholder changed identity.", effect="unknown")
                expected_ids.update((task_pane, parking_pane))
            actual_ids = {str(pane["pane_id"]) for _window, pane in flattened}
            if actual_ids != expected_ids:
                raise ShellCommandError("native_inventory_unknown", "The exact native canvas inventory contains an untracked pane.", effect="unknown")
            return dict(inventory) | {"windows": inventory_windows}
        status_windows = [window for window in windows
                          if window.get("window_id") == session.get("status_window_id")]
        if len(status_windows) != 1:
            raise ShellCommandError("native_inventory_unknown", "The status window identity changed.", effect="unknown")
        status = status_windows[0]
        status_panes = status.get("panes", [])
        if _layout_mode(session) == _CANVAS_LAYOUT:
            if (len(windows) != 1 or status.get("metadata", {}).get("status") != "1"
                    or status.get("metadata", {}).get("session") != session.get("handle")
                    or status.get("pane_count") != 1 or len(status_panes) != 1
                    or status_panes[0].get("pane_id") != session.get("status_pane_id")
                    or status_panes[0].get("pane_pid") != session.get("status_pane_pid")
                    or status_panes[0].get("dead") is not False
                    or status_panes[0].get("input_off") is not True):
                raise ShellCommandError("native_inventory_unknown", "The exact canvas renderer changed.", effect="unknown")
            for operation in operations:
                state = operation.get("state")
                if state in {"completed", "failed", "collected"}:
                    if not self._terminal_ops_collected((operation,)):
                        raise ShellCommandError("operation_records_unknown", "A canvas result lacks a drain receipt.", effect="unknown")
                elif state in {"intent", "running", "draining"} and (allow_active or operation.get("operation_id") == pending_operation_id):
                    if state != "intent" and operation.get("effect_certainty") != "known":
                        raise ShellCommandError("operation_records_unknown", "The canvas operation is uncertain.", effect="unknown")
                else:
                    raise ShellCommandError("operation_records_unknown", "An uncertain canvas operation blocks lifecycle change.", effect="unknown")
            return TmuxCommandBackend.inventory_identity(observed)
        if _layout_mode(session) == _FAMILY_LAYOUT:
            if (status.get("metadata", {}).get("status") != "1"
                    or status.get("metadata", {}).get("session") != session.get("handle")
                    or status.get("pane_count") != len(status_panes)
                    or status.get("pane_count", 0) < 1):
                raise ShellCommandError("native_inventory_unknown", "The family overview window identity changed.", effect="unknown")
            anchors = [pane for pane in status_panes
                       if pane.get("pane_id") == session.get("status_pane_id")]
            if len(anchors) != 1 or anchors[0].get("dead") is not True:
                raise ShellCommandError("native_inventory_unknown", "The family overview anchor is no longer inert.", effect="unknown")
            expected: dict[str, Mapping[str, Any]] = {}
            for operation in operations:
                state = operation.get("state")
                if state in {"completed", "failed", "collected"}:
                    if not self._terminal_ops_collected((operation,)):
                        raise ShellCommandError("operation_records_unknown", "A terminal command has uncollected work or output.", effect="unknown")
                elif (pending_operation_id is not None
                      and operation.get("operation_id") == pending_operation_id
                      and (state == "intent" or
                           (state == "running" and operation.get("effect_certainty") == "known"
                            and type(operation.get("dispatch_ack_at")) in {int, float}))):
                    pass
                elif allow_active and state in {"running", "draining"}:
                    if (operation.get("effect_certainty") != "known"
                            or operation.get("process_scope") not in {"tracking", "draining", "collected"}
                            or (state == "running" and operation.get("process_scope") != "tracking")):
                        raise ShellCommandError("operation_records_unknown", "An active command lacks durable supervision proof.", effect="unknown")
                else:
                    raise ShellCommandError("operation_records_unknown", "An active or uncertain command blocks lifecycle changes.", effect="unknown")
                if operation.get("cleanup_state") == "removed":
                    continue
                pane_id = operation.get("pane_id")
                if (not isinstance(pane_id, str) or pane_id == session.get("status_pane_id")
                        or pane_id in expected or operation.get("window_id") != session.get("status_window_id")):
                    raise ShellCommandError("operation_records_unknown", "Family pane identity is ambiguous.", effect="unknown")
                expected[pane_id] = operation
            seen: set[str] = set()
            for window in windows:
                window_id = window.get("window_id")
                if window_id != session.get("status_window_id"):
                    raise ShellCommandError("native_inventory_unknown", "An unowned window occupies the family server.", effect="unknown")
                for pane in window.get("panes", []):
                    pane_id = pane.get("pane_id")
                    if pane_id == session.get("status_pane_id"):
                        continue
                    operation = expected.get(pane_id)
                    metadata = pane.get("metadata", {})
                    if (operation is None or pane_id in seen
                            or metadata.get("session") != session.get("handle")
                            or metadata.get("operation") != operation.get("operation_id")
                            or metadata.get("request") != operation.get("request_digest")
                            or metadata.get("label") != operation.get("pane_label")):
                        raise ShellCommandError("native_inventory_unknown", "An unknown pane occupies the family window.", effect="unknown")
                    seen.add(str(pane_id))
                    if operation.get("operation_id") == pending_operation_id:
                        if pane.get("dead") or type(pane.get("pane_pid")) is not int:
                            raise ShellCommandError("native_inventory_unknown", "The new family pane failed process qualification.", effect="unknown")
                        continue
                    runner_pid = operation.get("runner_pid")
                    if type(runner_pid) is not int or pane.get("pane_pid") != runner_pid:
                        raise ShellCommandError("native_inventory_unknown", "The family command pane process identity changed.", effect="unknown")
                    if operation.get("state") in {"running", "draining"} or not pane.get("dead"):
                        runner = process_info(runner_pid)
                        if (runner is None or runner.get("starttime") != operation.get("runner_starttime")
                                or process_state(runner_pid) in {None, "Z", "X"}):
                            raise ShellCommandError("native_inventory_unknown", "The family command runner no longer matches its pane.", effect="unknown")
            if seen != set(expected):
                raise ShellCommandError("native_inventory_unknown", "A recorded family operation pane is missing.", effect="unknown")
            return TmuxCommandBackend.inventory_identity(observed)
        if (status.get("metadata", {}).get("status") != "1"
                or status.get("metadata", {}).get("session") != session.get("handle")
                or status.get("pane_count") != 1 or len(status_panes) != 1
                or status_panes[0].get("pane_id") != session.get("status_pane_id")
                or status_panes[0].get("dead") is not True):
            raise ShellCommandError("native_inventory_unknown", "The status pane is no longer an inert owned pane.", effect="unknown")
        by_window: dict[str, Mapping[str, Any]] = {}
        for operation in operations:
            state = operation.get("state")
            if state in {"completed", "failed", "collected"}:
                if not self._terminal_ops_collected((operation,)):
                    raise ShellCommandError("operation_records_unknown", "A terminal command has uncollected work or output.", effect="unknown")
            elif allow_active and state in {"running", "draining"}:
                if (operation.get("effect_certainty") != "known"
                        or operation.get("process_scope") not in {"tracking", "draining", "collected"}
                        or (state == "running" and operation.get("process_scope") != "tracking")):
                    raise ShellCommandError("operation_records_unknown", "An active command lacks durable supervision proof.", effect="unknown")
            else:
                raise ShellCommandError("operation_records_unknown", "An active or uncertain command blocks lifecycle changes.", effect="unknown")
            window_id = operation.get("window_id")
            if window_id is not None:
                if not isinstance(window_id, str) or window_id in by_window:
                    raise ShellCommandError("operation_records_unknown", "Command window identity is ambiguous.", effect="unknown")
                by_window[window_id] = operation
        seen: set[str] = set()
        for window in windows:
            window_id = window.get("window_id")
            if window_id == session.get("status_window_id"):
                continue
            operation = by_window.get(window_id)
            panes = window.get("panes", [])
            if (operation is None or window_id in seen or window.get("pane_count") != 1
                    or len(panes) != 1 or panes[0].get("pane_id") != operation.get("pane_id")
                    or window.get("metadata", {}).get("session") != session.get("handle")
                    or window.get("metadata", {}).get("operation") != operation.get("operation_id")
                    or window.get("metadata", {}).get("request") != operation.get("request_digest")):
                raise ShellCommandError("native_inventory_unknown", "An unknown command pane occupies the private server.", effect="unknown")
            seen.add(str(window_id))
            pane = panes[0]
            runner_pid = operation.get("runner_pid")
            if type(runner_pid) is not int or pane.get("pane_pid") != runner_pid:
                raise ShellCommandError("native_inventory_unknown", "The command pane process identity changed.", effect="unknown")
            if operation.get("state") in {"running", "draining"} or not pane.get("dead"):
                runner = process_info(runner_pid)
                if (runner is None or runner.get("starttime") != operation.get("runner_starttime")
                        or pane.get("pane_pid") != runner_pid
                        or process_state(runner_pid) in {None, "Z", "X"}):
                    raise ShellCommandError("native_inventory_unknown", "The command runner no longer matches its pane.", effect="unknown")
        if allow_active and any(op.get("state") in {"running", "draining"}
                                and op.get("window_id") not in seen for op in operations):
            raise ShellCommandError("native_inventory_unknown", "An active command pane is missing.", effect="unknown")
        return TmuxCommandBackend.inventory_identity(observed)

    @staticmethod
    def _matching_operation_panes(
        native: Mapping[str, Any], session: Mapping[str, Any], operation: Mapping[str, Any],
    ) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
            inventory = native.get("inventory", {})
            for window in inventory.get("windows", []) if isinstance(inventory, Mapping) else []:
                for pane in window.get("panes", []) if isinstance(window, Mapping) else []:
                    if (isinstance(pane, Mapping)
                            and pane.get("role") == "operation"
                            and pane.get("operation_id") == operation.get("operation_id")
                            and isinstance(operation.get("native_canvas"), Mapping)
                            and pane.get("pane_id") == operation["native_canvas"].get("pane_id")):
                        matches.append((window, pane))
            return matches
        family = _layout_mode(session) == _FAMILY_LAYOUT
        for window in native.get("windows", []):
            if window.get("window_id") != operation.get("window_id"):
                continue
            if family:
                for pane in window.get("panes", []):
                    metadata = pane.get("metadata", {})
                    if (pane.get("pane_id") == operation.get("pane_id")
                            and metadata.get("session") == session.get("handle")
                            and metadata.get("operation") == operation.get("operation_id")
                            and metadata.get("request") == operation.get("request_digest")
                            and metadata.get("label") == operation.get("pane_label")):
                        matches.append((window, pane))
            elif (window.get("metadata", {}).get("session") == session.get("handle")
                    and window.get("metadata", {}).get("operation") == operation.get("operation_id")
                    and window.get("metadata", {}).get("request") == operation.get("request_digest")):
                matches.extend((window, pane) for pane in window.get("panes", [])
                               if pane.get("pane_id") == operation.get("pane_id"))
        return matches

    def _paths(self, directory: Path, op_id: str) -> dict[str, Path]:
        # Encode the complete identifier in a dot-free filename stem. This
        # prevents both dotted IDs and IDs ending in a sidecar suffix from
        # naming another operation's record or payload.
        stem = "op-" + base64.urlsafe_b64encode(op_id.encode("ascii")).decode("ascii").rstrip("=")
        base = directory / "operations" / stem
        def suffixed(suffix: str) -> Path:
            return base.with_name(base.name + suffix)
        return {
            "record": suffixed(".json"), "payload": suffixed(".payload.json"),
            "release": suffixed(".release.json"), "log": suffixed(".log"),
            "cursor": suffixed(".cursor.json"), "drained": suffixed(".drained.json"),
            "finalize": suffixed(".finalize.json"),
            "inputs": directory / "operations" / f"{stem}.inputs",
        }

    def _refresh_telemetry(self, directory: Path, session: Mapping[str, Any]) -> None:
        updater = getattr(self.backend, "update_telemetry", None)
        if not callable(updater):
            return
        operations = self._strict_ops(directory, session)
        active = sum(item.get("state") not in {"completed", "failed", "collected"}
                     for item in operations)
        done = sum(item.get("state") in {"completed", "failed", "collected"}
                   for item in operations)
        output = 0
        input_state = "RO"
        for item in operations:
            seen = max(0, int(item.get("output_bytes_seen") or 0))
            cursor_path = self._paths(directory, str(item["operation_id"]))["cursor"]
            if cursor_path.exists():
                with log_lock(cursor_path):
                    cursor = read_json(cursor_path, limit=4096)
                if type(cursor.get("end_offset")) is int:
                    seen = max(seen, cursor["end_offset"])
            output += seen
            lease = item.get("input_lease")
            if (item.get("state") == "running" and isinstance(lease, dict)
                    and not lease.get("revoked")
                    and lease.get("machine_generation") == session.get("machine_generation")
                    and type(lease.get("deadline_monotonic")) in {int, float}
                    and time.monotonic() < lease["deadline_monotonic"]):
                input_state = "REQUESTED"
        events = [float(session.get("created_at", 0))]
        for item in operations:
            for key in ("dispatch_ack_at", "finished_at"):
                value = item.get(key)
                if type(value) in {int, float}:
                    events.append(float(value))
        started = session.get("created_monotonic")
        elapsed = (max(0, int(time.monotonic() - started))
                   if type(started) in {int, float} else
                   max(0, int(self.clock() - float(session.get("created_at", 0)))))
        updater(session, {"active": str(active), "done": str(done),
                          "output_bytes": str(output), "elapsed": str(elapsed),
                          "input_state": input_state})

    def _runner_command(self, paths: Mapping[str, Path]) -> list[str]:
        entries = [str(self.source_root), *(p for p in sys.path if p)]
        if os.environ.get("PYTHONPATH"):
            entries.append(os.environ["PYTHONPATH"])
        return ["env", "PYTHONPATH=" + os.pathsep.join(dict.fromkeys(entries)), sys.executable,
                "-m", "ls.core.terminal_ops.command_runner", "--operation", str(paths["record"]),
                "--payload", str(paths["payload"]), "--release", str(paths["release"]),
                "--drained", str(paths["drained"]), "--finalize", str(paths["finalize"])]

    def _capture_command(self, paths: Mapping[str, Path], session: Mapping[str, Any]) -> list[str]:
        entries = [str(self.source_root), *(p for p in sys.path if p)]
        if os.environ.get("PYTHONPATH"):
            entries.append(os.environ["PYTHONPATH"])
        return ["env", "PYTHONPATH=" + os.pathsep.join(dict.fromkeys(entries)), sys.executable,
                "-m", "ls.core.terminal_ops.command_capture",
                "--log", str(paths["log"]), "--cursor", str(paths["cursor"]),
                "--drained", str(paths["drained"]),
                "--max-bytes", str(session["settings"]["log_max_bytes"])]

    def run(
        self, handle: str, operation_id: str, *, argv: Sequence[str] | None = None,
        script: str | None = None, cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None, stdin: bytes | str | None = None,
        owner: Mapping[str, Any] | None = None,
        controlled_app: str | None = None,
        expanded_min_rows: int | None = None,
    ) -> dict[str, Any]:
        if expanded_min_rows is not None and (type(expanded_min_rows) is not int or not 2 <= expanded_min_rows <= 100):
            raise ShellCommandError("invalid_expanded_minimum", "Expanded height must be 2 through 100 rows including title.")
        op_id = validate_identifier(operation_id, "operation_id", maximum=128)
        if (argv is None) == (script is None):
            raise ShellCommandError("invalid_command", "Supply exactly one argv vector or explicit Bash script.")
        if argv is not None:
            if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv or len(argv) > 4096:
                raise ShellCommandError("invalid_argv", "The command argv must be a non-empty bounded string vector.")
            command: dict[str, Any] = {"argv": list(argv)}
            if any(not isinstance(a, str) or "\0" in a for a in command["argv"]):
                raise ShellCommandError("invalid_argv", "Every argv item must be a string without NUL characters.")
        else:
            if not isinstance(script, str) or "\0" in script or len(script.encode()) > 4_000_000:
                raise ShellCommandError("invalid_script", "The explicit Bash script must be bounded text without NUL.")
            command = {"script": script}
        if controlled_app is not None:
            if controlled_app != "less" or script is not None or stdin is not None or env:
                raise ShellCommandError("controlled_app_invalid", "The controlled pager needs one file and no custom environment or stdin.")
            if argv is None or len(argv) != 1:
                raise ShellCommandError("controlled_app_invalid", "The controlled pager needs one file path.")
            binary = shutil.which("less")
            if binary is None:
                raise ShellCommandError("controlled_app_unavailable", "The less pager is unavailable.")
            file_path = Path(argv[0]).expanduser().resolve(strict=True)
            if not file_path.is_file():
                raise ShellCommandError("controlled_app_invalid", "The pager target must be a regular file.")
            command = {"argv": [str(Path(binary).resolve(strict=True)), "-K", "-L", str(file_path)],
                       "controlled_app": "less"}
        try:
            workdir = (Path(cwd).expanduser() if cwd is not None else Path.cwd()).resolve(strict=True)
        except OSError as exc:
            raise ShellCommandError("invalid_cwd", "The command working directory is unavailable.") from exc
        if not workdir.is_dir():
            raise ShellCommandError("invalid_cwd", "The command working directory must be a directory.")
        explicit_env: dict[str, str] = {}
        if env is not None:
            if not isinstance(env, Mapping) or len(env) > 4096:
                raise ShellCommandError("invalid_env", "Command environment must be a bounded string map.")
            for key, value in env.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                        or not isinstance(value, str) or "\0" in value or key.endswith("_SUDO_KEY")):
                    raise ShellCommandError("invalid_env", "The environment contains an invalid or credential variable.")
                explicit_env[key] = value
        if isinstance(stdin, str):
            stdin_bytes = stdin.encode()
        elif stdin is None or isinstance(stdin, bytes):
            stdin_bytes = stdin
        else:
            raise ShellCommandError("invalid_stdin", "Command stdin must be bytes or text.")
        if stdin_bytes is not None and len(stdin_bytes) > 16 * 1024 * 1024:
            raise ShellCommandError("invalid_stdin", "Command stdin exceeds the supported size.")
        request = {**command, "cwd": str(workdir), "env": explicit_env,
                   "inherited_pythonpath": os.environ.get("PYTHONPATH"),
                   "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii") if stdin_bytes is not None else None}
        if len(json.dumps(request, ensure_ascii=True).encode("utf-8")) > 15 * 1024 * 1024:
            raise ShellCommandError("invalid_payload", "The complete command request exceeds the supported size.")
        request_digest = _digest(request)
        legacy_request_digest = _digest({key: value for key, value in request.items()
                                         if key != "inherited_pythonpath"})
        directory, session, op_from_handle = self._load(handle)
        if op_from_handle is not None:
            raise ShellCommandError("session_handle_required", "Starting a command requires a session handle.")
        if _family_like(session):
            caller, caller_role, caller_slug, caller_fingerprint = self._family_caller(session, owner, require_root_live=True)
        else:
            caller = self._authorize(session, owner)
            caller_role, caller_slug, caller_fingerprint = "root", "root", owner_fingerprint(caller)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            if _layout_mode(session) == _CANVAS_LAYOUT and controlled_app is not None:
                raise ShellCommandError("canvas_fullscreen_unsupported", "Controlled full-screen applications are not qualified in the canvas.")
            if _family_like(session):
                caller, caller_role, caller_slug, caller_fingerprint = self._family_caller(session, owner, require_root_live=True)
            else:
                caller = self._authorize(session, owner)
                caller_role, caller_slug, caller_fingerprint = "root", "root", owner_fingerprint(caller)
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                raise ShellCommandError("session_not_ready", "The exact owned shell session is not ready.", effect="unknown")
            paths = self._paths(directory, op_id)
            if paths["record"].exists():
                old = read_json(paths["record"], limit=2_000_000)
                # Earlier receipts did not include inherited PYTHONPATH in the
                # request digest. Recognize that exact old request, but always
                # return its original receipt rather than dispatching again.
                if old.get("request_digest") not in {request_digest, legacy_request_digest}:
                    raise ShellCommandError("operation_id_conflict", "This operation ID already names different command data.")
                self._authorize_operation(session, old, owner)
                return self._operation_reply(session, self._reconcile(directory, session, old),
                                             directory, owner=owner)
            existing = self._strict_ops(directory, session)
            if _family_like(session):
                for previous in existing:
                    if caller_role == "root" or previous.get("member_fingerprint") == caller_fingerprint:
                        self._reconcile(directory, session, previous)
                existing = self._strict_ops(directory, read_json(directory / "session.json"))
                session = read_json(directory / "session.json")
                if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                    raise ShellCommandError("session_not_ready", "The exact family session is not ready.", effect="unknown")
                native = self._verify(session)
                self._native_inventory(session, native, existing, allow_active=True)
                status_windows = [window for window in native.get("windows", [])
                                  if window.get("window_id") == session.get("status_window_id")]
                if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT}:
                    replacement = None
                else:
                    pane_count = len(status_windows[0].get("panes", [])) if len(status_windows) == 1 else 0
                    viewport_height = session.get("primary_viewport_height", _FAMILY_DEFAULT_ROWS)
                    if type(viewport_height) is not int or not 1 <= viewport_height <= 512:
                        raise ShellCommandError("family_viewport_unknown", "The qualified family viewport height is unavailable.", effect="unknown")
                    max_operations = _family_capacity(viewport_height)
                    retained = [item for item in existing
                                if (item.get("cleanup_state") == "retained"
                                    and item.get("state") == "completed"
                                    and self._terminal_ops_collected((item,))
                                    and any(pane.get("pane_id") == item.get("pane_id")
                                            and pane.get("dead") is True
                                            for pane in status_windows[0].get("panes", [])))]
                    if (pane_count < 1 or len(retained) > 1
                            or pane_count - 1 - len(retained) >= max_operations):
                        raise ShellCommandError("family_capacity", "The family viewport has no readable row capacity for another operation pane.")
                    replacement = retained[0] if retained else None
            else:
                replacement = None
                active = [op for op in existing
                          if op.get("state") not in {"completed", "failed", "collected"}]
                if active:
                    raise ShellCommandError("operation_busy", "A command is active or has an uncertain outcome.", effect="unknown")
            native_new_weights: dict[str, int] | None = None
            if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
                preflight_layout = _validate_native_layout(session.get("native_layout"))
                if preflight_layout.get("layout_state") in {"applying", "dispatching", "unknown"}:
                    raise ShellCommandError("native_layout_unknown",
                                            "An uncertain native placement must be reconciled before allocating another pane.",
                                            effect="unknown")
                saved_inspection = preflight_layout.get("inspection_restore")
                effective_mode = (saved_inspection.get("mode") if isinstance(saved_inspection, Mapping)
                                  else preflight_layout["mode"])
                if effective_mode == "fixed":
                    try:
                        native_new_weights = default_weights_for_new_operation(
                            saved_inspection.get("weights", {}) if isinstance(saved_inspection, Mapping)
                            else preflight_layout["weights"],
                            list(saved_inspection.get("visible_operation_ids", [])) if isinstance(saved_inspection, Mapping)
                            else list(preflight_layout.get("visible_operation_ids", [])), op_id)
                    except ValueError as exc:
                        raise ShellCommandError("native_ratio_limit",
                                                "The new pane cannot preserve exact fixed ratios within their supported range.") from exc
            paths["inputs"].mkdir(mode=0o700, exist_ok=True)
            op: dict[str, Any] = {
                "schema_version": 1, "operation_id": op_id,
                "layout_mode": _layout_mode(session),
                "operation_handle": self._op_handle(session["scope_key"], session["session_id"], op_id),
                "session_handle": session["handle"], "session_key": session["session_key"],
                "machine_generation": session["machine_generation"],
                "owner_fingerprint": caller_fingerprint, "member_fingerprint": caller_fingerprint,
                "member_label": "root" if caller_role == "root" else f"subagent {caller_slug}",
                "request_digest": request_digest,
                "controlled_app": controlled_app,
                "controlled_argv": command.get("argv") if controlled_app else None,
                "native_window_name": f"op-{op_id[:65]}-{uuid.uuid4().hex[:8]}",
                "cwd": str(workdir), "state": "intent", "effect_certainty": "unknown",
                "created_at": self.clock(), "updated_at": self.clock(), "cleanup_state": "pending",
                "output_drain": "pending",
            }
            if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
                expanded_minimum = (expanded_min_rows if expanded_min_rows is not None else
                                    session["runtime_settings"]["layout"]["expanded_min_rows"])
                op["expanded_min_rows"] = expanded_minimum
                # The configured minimum includes the pane's one-row title.
                # Parked native applications retain their separate physical floor.
                op["native_min_rows"] = max(
                    _NATIVE_MIN_PANE_ROWS,
                    expanded_minimum - 1,
                )
            if _layout_mode(session) == _FAMILY_LAYOUT:
                # This record predates any family-pane removal. Missing markers
                # in older records do not prove that no removal was attempted.
                op["family_removal_intent"] = "none"
            if _family_like(session):
                canvas = session["runtime_settings"]["layout"]["canvas_columns"]
                op["pane_label"] = _pane_label(caller_slug, op_id, str(workdir),
                                                root=caller_role == "root", width=canvas)
                if replacement is not None:
                    op.update(replaces_pane_id=replacement["pane_id"],
                              replaces_operation_id=replacement["operation_id"],
                              replaces_request_digest=replacement["request_digest"],
                              replaces_pane_label=replacement["pane_label"])
            else:
                op["pane_label"] = _pane_label(caller_slug, op_id, str(workdir),
                                                root=caller_role == "root")
            atomic_json(paths["record"], op, exclusive=True)
            atomic_json(paths["payload"], request, exclusive=True)
            try:
                if _layout_mode(session) == _CANVAS_LAYOUT:
                    session["activation_sequence"] = int(session.get("activation_sequence", 0)) + 1
                    op.update(activation_seq=session["activation_sequence"],
                              activated_monotonic=time.monotonic(),
                              expanded_min_rows=expanded_min_rows or session["runtime_settings"]["layout"]["expanded_min_rows"],
                              window_id=session["status_window_id"])
                    atomic_json(paths["record"], op)
                    runner_command = self._runner_command(paths)
                    process = subprocess.Popen(runner_command, cwd=str(workdir),
                                               stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL,
                                               env=self.backend._environment(),
                                               shell=False, start_new_session=True,
                                               close_fds=True)
                    runner = process_info(process.pid)
                    if runner is None:
                        raise ShellCommandError("runner_identity_unknown", "The canvas runner did not expose its process identity.", effect="unknown")
                    op.update(state="running", effect_certainty="known", runner_pid=process.pid,
                              runner_starttime=runner["starttime"],
                              dispatch_ack_at=self.clock(), updated_at=self.clock())
                    atomic_json(paths["record"], op)
                    atomic_json(paths["release"], {"state": "release", "released_at": self.clock()}, exclusive=True)
                    session.update(last_activity_at=self.clock(), last_activity_kind="run")
                    atomic_json(directory / "session.json", session)
                    return self._operation_reply(session, op, directory, owner=owner)
                if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
                    layout_state = _validate_native_layout(session.get("native_layout"))
                    session["activation_sequence"] = int(session.get("activation_sequence", 0)) + 1
                    op.update(activation_seq=session["activation_sequence"],
                              layout_order=len(layout_state.get("operation_order", [])),
                              activated_monotonic=time.monotonic())
                    layout_state["operation_order"].append(op_id)
                    saved_inspection = layout_state.get("inspection_restore")
                    if isinstance(saved_inspection, Mapping):
                        saved_inspection = dict(saved_inspection)
                        saved_visible = list(saved_inspection.get("visible_operation_ids", []))
                        if saved_inspection.get("mode") in {"stable", "fixed"} and op_id not in saved_visible:
                            saved_visible.append(op_id)
                        saved_inspection["visible_operation_ids"] = saved_visible
                        if saved_inspection.get("mode") == "fixed":
                            assert native_new_weights is not None
                            saved_inspection["weights"] = native_new_weights
                        layout_state["inspection_restore"] = saved_inspection
                    else:
                        existing_visible = list(layout_state.get("visible_operation_ids", []))
                        if layout_state["mode"] in {"stable", "fixed"} and op_id not in existing_visible:
                            existing_visible.append(op_id)
                        layout_state["visible_operation_ids"] = existing_visible
                        if layout_state["mode"] == "fixed":
                            assert native_new_weights is not None
                            layout_state["weights"] = native_new_weights
                    if layout_state["mode"] == "follow" and layout_state.get("selected_operation_id") is None:
                        layout_state["selected_operation_id"] = op_id
                        layout_state["last_automatic_change_monotonic"] = time.monotonic()
                    layout_state["desired_generation"] += 1
                    layout_state["pending"] = True
                    layout_state["layout_state"] = "desired"
                    layout_state["last_activation_monotonic"] = time.monotonic()
                    session["native_layout"] = layout_state
                    session.update(follow_event_operation_id=op_id,
                                   follow_event_monotonic=time.monotonic(),
                                   last_activity_at=self.clock(), last_activity_kind="run")
                    op.update(state="intent", effect_certainty="unknown")
                    atomic_json(paths["record"], op)
                    atomic_json(directory / "session.json", session)
                    native = self.backend.create_native_operation(
                        session, op, command=self._runner_command(paths),
                        pane_rows=op["native_min_rows"],
                    )
                    if not isinstance(native, Mapping):
                        raise ShellCommandError("native_identity_unknown", "The native operation pane identity is unavailable.", effect="unknown")
                    native_canvas = native.get("native_canvas", native)
                    required_native = {"window_id", "pane_id", "parking_window_id",
                                       "parking_pane_id", "pane_rows"}
                    if not isinstance(native_canvas, Mapping) or not required_native.issubset(native_canvas):
                        raise ShellCommandError("native_identity_unknown", "The native operation did not return its workload and parking identities.", effect="unknown")
                    op["native_canvas"] = dict(native_canvas)
                    op.update(window_id=native_canvas["window_id"], pane_id=native_canvas["pane_id"])
                    op.update({key: value for key, value in native.items() if key != "native_canvas"})
                    atomic_json(paths["record"], op)
                    operations = self._strict_ops(directory, session)
                    observed = self._verify(session)
                    self._native_inventory(session, observed, operations,
                                           allow_active=True, pending_operation_id=op_id)
                    self.backend.start_capture(session, op, command=self._capture_command(paths, session))
                    op.update(state="running", effect_certainty="known",
                              dispatch_ack_at=self.clock(), updated_at=self.clock())
                    atomic_json(paths["record"], op)
                    atomic_json(paths["release"], {"state": "release", "released_at": self.clock()}, exclusive=True)
                    return self._operation_reply(session, op, directory, owner=owner)
                native = self.backend.create_operation_window(session, op, command=self._runner_command(paths))
                op.update(native)
                if _layout_mode(session) == _FAMILY_LAYOUT:
                    atomic_json(paths["record"], op)
                self.backend.tag_operation_window(session, op, native)
                if _layout_mode(session) == _FAMILY_LAYOUT:
                    operations = self._strict_ops(directory, session)
                    native_before_layout = self._verify(session)
                    inventory = self._native_inventory(
                        session, native_before_layout, operations,
                        allow_active=True, pending_operation_id=op_id)
                    windows = inventory.get("windows", [])
                    if len(windows) != 1:
                        raise ShellCommandError("native_inventory_unknown", "The family window inventory is ambiguous.", effect="unknown")
                    if replacement is None:
                        self.backend.arrange_family_window(
                            session, expected_window=windows[0])
                capture_command = [
                    "env", "PYTHONPATH=" + os.pathsep.join((str(self.source_root), *[p for p in sys.path if p])),
                    sys.executable, "-m", "ls.core.terminal_ops.command_capture",
                    "--log", str(paths["log"]), "--cursor", str(paths["cursor"]),
                    "--drained", str(paths["drained"]),
                    "--max-bytes", str(session["settings"]["log_max_bytes"]),
                ]
                self.backend.start_capture(session, op, command=capture_command)
                self.backend.select_operation(session, op)
                op.update(state="running", effect_certainty="known",
                          dispatch_ack_at=self.clock(), updated_at=self.clock())
                atomic_json(paths["record"], op)
                atomic_json(paths["release"], {"state": "release", "released_at": self.clock()}, exclusive=True)
                session.update(last_activity_at=self.clock(), last_activity_kind="run")
                atomic_json(directory / "session.json", session)
            except Exception as exc:
                op.update(state="unknown", effect_certainty="unknown",
                          dispatch_error=getattr(exc, "code", exc.__class__.__name__),
                          updated_at=self.clock())
                try:
                    atomic_json(paths["record"], op)
                except ShellCommandError:
                    pass
                if isinstance(exc, ShellCommandError):
                    raise ShellCommandError(exc.code, exc.message, effect="unknown") from exc
                raise ShellCommandError("native_ack_unknown", "Dispatch acknowledgement is uncertain; the command was not replayed.", effect="unknown") from exc
            if replacement is not None:
                try:
                    self._reconcile(directory, session, replacement,
                                    accepted_operation_id=op_id)
                except (ShellCommandError, OSError):
                    # The new command is already accepted. A later status or
                    # finalizer can reconcile the exact old pane without replay.
                    pass
            try:
                self._refresh_telemetry(directory, session)
            except (ShellCommandError, OSError, ValueError):
                pass
            return self._operation_reply(session, op, directory, owner=owner)

    def _operation_reply(
        self, session: Mapping[str, Any], operation: Mapping[str, Any], directory: Path,
        *, include_output: bool = True, owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        handle = str(operation.get("operation_handle") or self._op_handle(
            str(session["scope_key"]), str(session["session_id"]), str(operation["operation_id"])))
        session_join = self._shell_command("attach", "--session", str(session["native_name"]))
        pane_available = (operation.get("state") in {"running", "draining"}
                          and operation.get("process_scope") != "collected"
                          and bool(operation.get("pane_id"))
                          and (bool(operation.get("window_id"))
                               or isinstance(operation.get("native_canvas"), Mapping)))
        operation_join = (session_join if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT else
                          self._shell_command("attach", "--handle", handle)
                          if pane_available else None)
        result = _reply(
            str(operation.get("state", "unknown")), effect=str(operation.get("effect_certainty", "unknown")),
            handle=handle, operation_handle=handle, session_handle=session["handle"],
            operation_id=operation["operation_id"], request_digest=operation.get("request_digest"),
            member_label=operation.get("member_label"), pane_label=operation.get("pane_label"),
            exit_code=operation.get("exit_code"), process_scope=operation.get("process_scope", "unknown"),
            output_drain=operation.get("output_drain", "pending"),
            cleanup_state=operation.get("cleanup_state", "pending"),
            logs_command=self._shell_command("logs", "--handle", handle),
            machine={"host": session.get("host"), "generation": session.get("machine_generation")},
            native_session_name=session.get("server_session_name", session.get("native_name")),
            join_command=operation_join or session_join,
            operation_join_command=operation_join, session_join_command=session_join,
            join_target=("session" if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT
                         else "operation_pane" if pane_available else "session"),
            pane_available=pane_available,
            window_id=operation.get("window_id"), pane_id=operation.get("pane_id"),
            native_allocation_bytes=operation.get("native_allocation_bytes"),
            native_allocation_state=operation.get("native_allocation_state", "unobserved"),
            native_allocation_recovery=operation.get("native_allocation_recovery"),
            native_canvas=operation.get("native_canvas")
            if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT else None,
            desired_generation=(session.get("native_layout", {}).get("desired_generation")
                                if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT else None),
            applied_generation=(session.get("native_layout", {}).get("applied_generation")
                                if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT else None),
            native_history_retention=session.get("native_history_retention")
            if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT else None,
            input_lease=(lambda lease: ({"state": "requested" if not lease.get("revoked")
                                       and lease.get("machine_generation") == session.get("machine_generation")
                                       and time.monotonic() < lease.get("deadline_monotonic", 0)
                                       and operation.get("state") == "running" else "expired",
                                       "grantee": lease.get("grantee"), "expires_at": lease.get("expires_at"),
                                       "request_digest": lease.get("request_digest"),
                                       "pane_id": lease.get("pane_id"), "runner_pid": lease.get("runner_pid"),
                                       "native_observer_input": "disabled"}) if isinstance(lease, dict) else None)(
                                           operation.get("input_lease")),
        )
        # Include a small recent output window in the normal run/status payload
        # so a caller does not need a second command merely to see the result.
        # logs() reads a bounded tail and never drains or mutates the operation.
        if not include_output:
            return result
        try:
            recent = self.logs(handle, lines=min(session["settings"]["default_lines"], _AGENT_LINES),
                               max_bytes=min(session["settings"]["max_response_bytes"], _AGENT_OUTPUT_BYTES),
                               owner=owner)
            result.update(
                output=recent.get("output", ""),
                output_cursor=recent.get("cursor", 0),
                output_base_cursor=recent.get("base_cursor", 0),
                output_end_cursor=recent.get("end_cursor", 0),
                output_truncated=recent.get("truncated", False),
                output_gap=recent.get("gap", False),
                output_storage=recent.get("storage", False),
            )
        except (OSError, ShellCommandError):
            # Output storage is observational. A transient read failure must
            # not turn an already-recorded dispatch or status into failure.
            result.update(output="", output_unavailable=True, output_truncated=False)
        return result

    def _load_operation(self, handle: str) -> tuple[Path, dict[str, Any], dict[str, Any], Path]:
        directory, session, op_id = self._load(handle)
        if op_id is None:
            raise ShellCommandError("operation_handle_required", "This action requires an operation handle.")
        path = self._paths(directory, op_id)["record"]
        op = read_json(path, limit=2_000_000)
        if op.get("operation_id") != op_id or op.get("session_handle") != session.get("handle"):
            raise ShellCommandError("operation_identity_mismatch", "The operation handle does not match private state.", effect="unknown")
        return directory, session, op, path

    def _reconcile(
        self, directory: Path, session: dict[str, Any], op: dict[str, Any],
        *, prune_older: bool = True, accepted_operation_id: str | None = None,
    ) -> dict[str, Any]:
        if _layout_mode(session) == _CANVAS_LAYOUT:
            paths = self._paths(directory, str(op["operation_id"]))
            latest = read_json(paths["record"], limit=2_000_000)
            if latest.get("operation_id") != op.get("operation_id") or latest.get("session_key") != session.get("session_key"):
                raise ShellCommandError("operation_identity_mismatch", "The canvas operation record changed.", effect="unknown")
            if latest.get("state") in {"completed", "unknown"}:
                return latest
            runner_pid = latest.get("runner_pid")
            runner = process_info(runner_pid) if type(runner_pid) is int else None
            if (runner is None or runner.get("starttime") != latest.get("runner_starttime")
                    or process_state(runner_pid) in {None, "Z", "X"}):
                latest.update(state="unknown", effect_certainty="unknown", process_scope="unknown",
                              detail="the exact canvas runner exited without a completion receipt")
                atomic_json(paths["record"], latest)
            return latest
        # A completed, collected operation whose exact pane was already removed
        # has no native state left to reconcile. The family caller still checks
        # every receipt and the current native inventory before dispatch.
        op_id = op.get("operation_id")
        if (op.get("state") == "completed"
                and op.get("effect_certainty") == "known"
                and op.get("process_scope") == "collected"
                and op.get("output_drain") in {"available", "unavailable"}
                and op.get("cleanup_state") == "removed"
                and type(op.get("collected_at")) in {int, float}
                and isinstance(op_id, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", op_id)
                and op.get("session_handle") == session.get("handle")
                and op.get("session_key") == session.get("session_key")
                and op.get("operation_handle") == self._op_handle(
                    str(session["scope_key"]), str(session["session_id"]), op_id)
                and isinstance(op.get("request_digest"), str)
                and _HEX64.fullmatch(op["request_digest"])):
            return op
        telemetry_before = (op.get("state"), op.get("output_bytes_seen"))
        paths = self._paths(directory, str(op["operation_id"]))
        if op.get("state") not in {"completed", "failed", "collected"}:
            native = self._verify(session)
            matching_panes = self._matching_operation_panes(native, session, op)
            matches = [window for window, _pane in matching_panes]
            runner = process_info(op.get("runner_pid")) if type(op.get("runner_pid")) is int else None
            runner_state = process_state(int(op["runner_pid"])) if type(op.get("runner_pid")) is int else None
            runner_live = bool(runner and runner.get("starttime") == op.get("runner_starttime")
                               and runner_state is not None and runner_state not in {"Z", "X"})
            if not runner_live:
                try:
                    latest = read_json(paths["record"], limit=2_000_000)
                except ShellCommandError:
                    latest = {}
                if (latest.get("operation_id") == op.get("operation_id")
                        and latest.get("session_key") == op.get("session_key")
                        and latest.get("state") in {"completed", "failed", "unknown"}):
                    op = latest
            if op.get("state") in {"intent", "dispatching"}:
                op.update(state="unknown", effect_certainty="unknown",
                          detail="dispatch outcome is not durable; replay is prohibited",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif op.get("state") == "running" and not runner_live and (op.get("runner_pid") is not None or not matches):
                op.update(state="unknown", effect_certainty="unknown", process_scope="unknown",
                          detail="the exact runner exited without a completion receipt",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif (op.get("state") == "draining" and not runner_live
                  and op.get("process_scope") != "collected"):
                op.update(state="unknown", effect_certainty="unknown", process_scope="unknown",
                          detail="the process scope did not confirm collection",
                          reconciled_at=self.clock())
                atomic_json(paths["record"], op)
            elif op.get("state") == "draining" and op.get("process_scope") == "collected":
                drain = {}
                if paths["drained"].exists() and not paths["drained"].is_symlink():
                    try:
                        drain = read_json(paths["drained"], limit=4096)
                    except ShellCommandError:
                        drain = {}
                if drain.get("state") == "drained":
                    op["output_drain"] = drain.get("storage", "available")
                    op["output_bytes_seen"] = drain.get("bytes_seen")
                elif not op.get("capture_stop_requested_at"):
                    # The runner remains alive in the exact pane after all
                    # workload descendants are collected. Close its pipe here,
                    # then wait for the collector's EOF receipt.
                    try:
                        pane_matches = [
                            pane for window in matches
                            for pane in window.get("panes", [])
                            if pane.get("pane_id") == op.get("pane_id")
                            and pane.get("pane_pid") == op.get("runner_pid")
                            and not pane.get("dead")
                        ]
                        if len(pane_matches) == 1:
                            self.backend.stop_capture(session, op)
                        else:
                            op["capture_stop_state"] = "pane_identity_unknown"
                    except ShellCommandError:
                        op["capture_stop_state"] = "unknown"
                    op["capture_stop_requested_at"] = self.clock()
                elif self.clock() - float(op["capture_stop_requested_at"]) >= 3.0:
                    op["output_drain"] = "unavailable"
                    op["capture_stop_state"] = "collector_unavailable"
                if op.get("output_drain") in {"available", "unavailable"}:
                    if runner_live:
                        final_payload = {
                            "state": "finalize", "operation_id": op.get("operation_id"),
                            "session_key": session.get("session_key"),
                            "output_drain": op["output_drain"], "finalized_at": self.clock(),
                        }
                        if not paths["finalize"].exists():
                            try:
                                atomic_json(paths["finalize"], final_payload, exclusive=True)
                            except ShellCommandError:
                                # Another status call can only reach this under a
                                # separate process after taking the same lock.
                                pass
                    else:
                        # The runner durably proved its full descendant scope
                        # collected before it exited. A collector receipt (or
                        # bounded unavailability) is sufficient to finish the
                        # operation without replaying the command.
                        completed_at = op.get("command_finished_at")
                        if type(completed_at) not in {int, float}:
                            completed_at = self.clock()
                        drained_at = drain.get("drained_at")
                        if type(drained_at) in {int, float}:
                            completed_at = max(float(completed_at), float(drained_at))
                        op.update(state="completed", effect_certainty="known",
                                  finished_at=completed_at)
                atomic_json(paths["record"], op)
            if op.get("state") not in {"completed", "failed", "collected"}:
                op["native_window_state"] = (
                    "present" if matches else ("unknown" if native.get("state") != "ready" else "missing")
                )
        if op.get("state") in {"completed", "failed"}:
            drained = {}
            if paths["drained"].exists() and not paths["drained"].is_symlink():
                try:
                    drained = read_json(paths["drained"], limit=4096)
                except ShellCommandError:
                    drained = {}
            if drained.get("state") == "drained":
                op["output_drain"] = drained.get("storage", "available")
                op["output_bytes_seen"] = drained.get("bytes_seen")
            else:
                op["output_drain"] = "pending"
                if (op.get("window_id") and op.get("pane_id")
                        and not op.get("capture_stop_requested_at")):
                    try:
                        self.backend.stop_capture(session, op)
                        op["capture_stop_requested_at"] = self.clock()
                        atomic_json(paths["record"], op)
                    except ShellCommandError:
                        op["capture_stop_state"] = "unknown"
                elif (op.get("capture_stop_requested_at")
                      and self.clock() - float(op["capture_stop_requested_at"]) >= 3.0):
                    op["output_drain"] = "unavailable"
                    op["capture_stop_state"] = "collector_unavailable"
            if op.get("output_drain") in {"available", "unavailable"}:
                native = self._verify(session)
                candidates = [window for window in native.get("windows", [])
                              if window.get("window_id") == op.get("window_id")]
                if _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
                    # Native workload panes move between windows as placeholders
                    # absorb layout changes. Their persisted window id is only
                    # an initial hint, so reconcile by the immutable pane pair
                    # and never route them through the legacy kill_window path.
                    try:
                        operations = self._strict_ops(directory, session)
                        operations = [dict(op) if item.get("operation_id") == op.get("operation_id")
                                      else item for item in operations]
                        inventory = self._native_inventory(
                            session, native, operations, allow_active=True)
                        raw_inventory = native.get("inventory", {})
                        windows_by_id = {str(window.get("window_id")): window
                                         for window in raw_inventory.get("windows", [])
                                         if isinstance(window, Mapping)}
                        all_panes = [pane for window in raw_inventory.get("windows", [])
                                     if isinstance(window, Mapping)
                                     for pane in window.get("panes", [])
                                     if isinstance(pane, Mapping)]
                        native_identity = op.get("native_canvas")
                        if not isinstance(native_identity, Mapping):
                            raise ShellCommandError("native_inventory_unknown", "The completed native operation lost its pane pair.", effect="unknown")
                        task_id = native_identity.get("pane_id")
                        placeholder_id = native_identity.get("parking_pane_id")
                        pair_present = [pane for pane in all_panes
                                        if pane.get("pane_id") in {task_id, placeholder_id}]
                        if op.get("cleanup_state") in {"dispatching", "unknown", "removed"}:
                            if not pair_present and op.get("cleanup_state") in {"dispatching", "unknown", "removed"}:
                                op["cleanup_state"] = "removed"
                                op.setdefault("collected_at", self.clock())
                            elif op.get("cleanup_state") == "removed" or op.get("cleanup_state") == "dispatching":
                                # A lost acknowledgement is reconciled by readback
                                # only; never repeat the paired-window removal.
                                op["cleanup_state"] = "unknown"
                            else:
                                op["cleanup_state"] = "unknown"
                        elif len(pair_present) != 2:
                            op["cleanup_state"] = "unknown"
                        else:
                            task_matches = [pane for pane in pair_present
                                            if pane.get("pane_id") == task_id
                                            and pane.get("role") == "operation"
                                            and pane.get("operation_id") == op.get("operation_id")
                                            and pane.get("request") == op.get("request_digest")]
                            placeholder_matches = [pane for pane in pair_present
                                                   if pane.get("pane_id") == placeholder_id
                                                   and pane.get("role") in {"parking", "placeholder"}
                                                   and pane.get("operation_id") == op.get("operation_id")
                                                   and pane.get("request") == op.get("request_digest")]
                            if len(task_matches) != 1 or len(placeholder_matches) != 1:
                                raise ShellCommandError("native_inventory_unknown", "The native pane pair changed identity during cleanup.", effect="unknown")
                            task, placeholder = task_matches[0], placeholder_matches[0]
                            task_window = windows_by_id.get(str(task.get("window_id")), {})
                            placeholder_window = windows_by_id.get(str(placeholder.get("window_id")), {})
                            parked_pair = (
                                task.get("dead") is True
                                and task_window.get("role") == "parking"
                                and placeholder_window.get("role") in {"parking", "placeholder"}
                                and len(task_window.get("panes", [])) == 1
                                and len(placeholder_window.get("panes", [])) == 1
                                and task.get("window_id") != session.get("status_window_id")
                                and placeholder.get("window_id") != session.get("status_window_id")
                            )
                            remove_native = getattr(self.backend, "remove_native_operation", None)
                            layout = session.get("native_layout", {})
                            pending = layout.get("pending_follow_event") or {}
                            protected = {
                                layout.get("selected_operation_id"),
                                session.get("follow_event_operation_id"),
                                pending.get("operation_id"),
                                *layout.get("visible_operation_ids", []),
                            }
                            if (parked_pair and callable(remove_native)
                                    and op.get("operation_id") not in protected):
                                # Persist the mutation intent before dispatch. A
                                # retry can only inspect whether both panes are
                                # gone; it cannot issue the removal again.
                                op["cleanup_state"] = "dispatching"
                                atomic_json(paths["record"], op)
                                try:
                                    removed = remove_native(
                                        session, op, expected_inventory=native["inventory"])
                                except ShellCommandError as exc:
                                    op["cleanup_state"] = "unknown" if exc.effect == "unknown" else "retained"
                                    op["cleanup_error"] = exc.code
                                else:
                                    if removed.get("state") == "removed":
                                        after = self._verify(session)
                                        verified_ops = [dict(op) | {"cleanup_state": "removed"}
                                                        if item.get("operation_id") == op.get("operation_id")
                                                        else item for item in self._strict_ops(directory, session)]
                                        self._native_inventory(session, after, verified_ops,
                                                               allow_active=True)
                                        op["cleanup_state"] = "removed"
                                        op.setdefault("collected_at", self.clock())
                                    else:
                                        op["cleanup_state"] = "unknown"
                            else:
                                op["cleanup_state"] = "retained"
                    except ShellCommandError as exc:
                        op["cleanup_state"] = "unknown"
                        op["cleanup_error"] = exc.code
                elif _layout_mode(session) == _FAMILY_LAYOUT:
                    matching_panes = self._matching_operation_panes(native, session, op)
                    if len(candidates) == 1 and len(matching_panes) == 1:
                        window, pane = matching_panes[0]
                        runner_pid = op.get("runner_pid")
                        runner = process_info(runner_pid) if type(runner_pid) is int else None
                        exact = (
                            native.get("state") == "ready"
                            and op.get("process_scope") == "collected"
                            and window.get("window_id") == session.get("status_window_id")
                            and pane.get("pane_id") == op.get("pane_id")
                            and pane.get("pane_pid") == runner_pid
                            and (pane.get("dead") is True
                                 or (runner is not None
                                     and runner.get("starttime") == op.get("runner_starttime")
                                     and process_state(runner_pid) not in {None, "Z", "X"}))
                        )
                        if not exact:
                            op["cleanup_state"] = "unknown"
                        else:
                            kill_acknowledged = False
                            try:
                                operations = self._strict_ops(directory, session)
                                operations = [({**item, "output_drain": op["output_drain"]}
                                               if item.get("operation_id") == op.get("operation_id")
                                               else item) for item in operations]
                                self._native_inventory(session, native, operations, allow_active=True,
                                                       pending_operation_id=accepted_operation_id)
                                finished = op.get("finished_at")
                                finished_key = (float(finished) if type(finished) in {int, float} else 0.0,
                                                str(op["operation_id"]))
                                accepted = next((item for item in operations
                                                 if item.get("operation_id") == accepted_operation_id), None)
                                accepted_replacement = False
                                if (accepted is not None and accepted.get("state") == "running"
                                        and accepted.get("effect_certainty") == "known"
                                        and type(accepted.get("dispatch_ack_at")) in {int, float}
                                        and accepted.get("replaces_operation_id") == op.get("operation_id")
                                        and accepted.get("replaces_pane_id") == op.get("pane_id")
                                        and accepted.get("replaces_request_digest") == op.get("request_digest")
                                        and accepted.get("replaces_pane_label") == op.get("pane_label")):
                                    release_path = self._paths(directory, str(accepted_operation_id))["release"]
                                    if release_path.exists() and not release_path.is_symlink():
                                        release = read_json(release_path, limit=4096)
                                        accepted_replacement = (release.get("state") == "release"
                                                                and type(release.get("released_at")) in {int, float})
                                superseded = any(
                                    item.get("operation_id") != op.get("operation_id")
                                    and item.get("effect_certainty") == "known"
                                    and (
                                        (type(item.get("dispatch_ack_at")) in {int, float}
                                         and item["dispatch_ack_at"] > finished_key[0])
                                        or (item.get("state") == "completed"
                                            and type(item.get("finished_at")) in {int, float}
                                            and (float(item["finished_at"]), str(item["operation_id"])) > finished_key)
                                    ) for item in operations) or accepted_replacement
                                removal_intent = op.get("family_removal_intent")
                                no_prior_kill = (
                                    removal_intent == "none"
                                    or (removal_intent is None
                                        and op.get("cleanup_state") == "retained")
                                )
                                if not no_prior_kill:
                                    # Only a new record's explicit no-attempt
                                    # marker or a legacy retained result proves
                                    # no prior removal attempt. Legacy pending
                                    # may conceal an uncertain acknowledgement.
                                    op["cleanup_state"] = "unknown"
                                elif not superseded and pane.get("dead") is True:
                                    label_completed = getattr(self.backend, "label_completed_pane", None)
                                    if callable(label_completed):
                                        label_completed(session, op,
                                                        expected_window=TmuxCommandBackend.window_identity(window))
                                    op["cleanup_state"] = "retained"
                                elif not superseded:
                                    op["cleanup_state"] = "pending"
                                else:
                                    op["cleanup_state"] = "dispatching"
                                    op["family_removal_intent"] = "dispatching"
                                    atomic_json(paths["record"], op)
                                    self.backend.kill_window(
                                        session, op,
                                        expected_window=TmuxCommandBackend.window_identity(window))
                                    kill_acknowledged = True
                                    after = self._verify(session)
                                    remaining = self._matching_operation_panes(after, session, op)
                                    if remaining:
                                        op["cleanup_state"] = "unknown"
                                    else:
                                        operations = [({**item, "cleanup_state": "removed"}
                                                       if item.get("operation_id") == op.get("operation_id")
                                                       else item) for item in operations]
                                        self._native_inventory(session, after, operations, allow_active=True,
                                                               pending_operation_id=accepted_operation_id)
                                        after_windows = [window for window in after.get("windows", [])
                                                         if window.get("window_id") == session.get("status_window_id")]
                                        if len(after_windows) != 1:
                                            raise ShellCommandError("native_inventory_unknown", "The family window changed before rebalancing.", effect="unknown")
                                        self.backend.arrange_family_window(
                                            session,
                                            expected_window=TmuxCommandBackend.window_identity(after_windows[0]))
                                        after_layout = self._verify(session)
                                        self._native_inventory(session, after_layout, operations,
                                                               allow_active=True,
                                                               pending_operation_id=accepted_operation_id)
                                        op["cleanup_state"] = "removed"
                                        op["family_removal_intent"] = "removed"
                                        if op.get("native_allocation_state") == "over_budget":
                                            op["native_allocation_recovery"] = "pane_removed"
                            except ShellCommandError as exc:
                                # Only a proved pre-mutation failure can be
                                # retried. Once tmux may have received the kill,
                                # subsequent reconciliation is readback-only.
                                op["cleanup_state"] = (
                                    "pending" if exc.effect == "none" and not kill_acknowledged
                                    and op.get("cleanup_state") == "dispatching" else "unknown")
                                op["cleanup_error"] = exc.code
                                if op["cleanup_state"] == "pending":
                                    op["family_removal_intent"] = "none"
                                if op["cleanup_state"] == "unknown":
                                    # A lost acknowledgement can follow an
                                    # applied kill. Prove absence against the
                                    # whole family before settling the receipt.
                                    try:
                                        after = self._verify(session)
                                        if (after.get("state") == "ready"
                                                and not self._matching_operation_panes(after, session, op)):
                                            verified_ops = [
                                                ({**item, "cleanup_state": "removed",
                                                  "output_drain": op["output_drain"]}
                                                if item.get("operation_id") == op.get("operation_id")
                                                else item)
                                                for item in self._strict_ops(directory, session)
                                            ]
                                            self._native_inventory(session, after, verified_ops,
                                                                   allow_active=True)
                                            op["cleanup_state"] = "removed"
                                            op["family_removal_intent"] = "removed"
                                    except ShellCommandError:
                                        pass
                    elif not matching_panes and native.get("state") == "ready":
                        try:
                            operations = self._strict_ops(directory, session)
                            operations = [({**item, "cleanup_state": "removed", "output_drain": op["output_drain"]}
                                           if item.get("operation_id") == op.get("operation_id")
                                           else item) for item in operations]
                            self._native_inventory(session, native, operations, allow_active=True)
                            op["cleanup_state"] = "removed"
                            op["family_removal_intent"] = "removed"
                        except ShellCommandError as exc:
                            op["cleanup_state"] = "unknown"
                            op["cleanup_error"] = exc.code
                    else:
                        op["cleanup_state"] = "unknown"
                elif candidates:
                    window = candidates[0]
                    panes = window.get("panes", [])
                    pane = panes[0] if len(panes) == 1 else {}
                    runner_pid = op.get("runner_pid")
                    runner = process_info(runner_pid) if type(runner_pid) is int else None
                    exact = (
                        native.get("state") == "ready" and len(candidates) == 1
                        and op.get("process_scope") == "collected"
                        and window.get("metadata", {}).get("session") == session.get("handle")
                        and window.get("metadata", {}).get("operation") == op.get("operation_id")
                        and window.get("metadata", {}).get("request") == op.get("request_digest")
                        and window.get("pane_count") == 1 and len(panes) == 1
                        and pane.get("pane_id") == op.get("pane_id")
                        and type(runner_pid) is int and pane.get("pane_pid") == runner_pid
                        and (pane.get("dead") is True
                             or (runner is not None
                                 and runner.get("starttime") == op.get("runner_starttime")
                                 and process_state(runner_pid) not in {None, "Z", "X"}))
                    )
                    if not exact:
                        op["cleanup_state"] = "unknown"
                    else:
                        pins, pin_unknown = self._pins(directory)
                        attached = native.get("session", {}).get("attached")
                        if pins or pin_unknown or type(attached) is not int or attached != 0:
                            op["cleanup_state"] = "pending"
                        else:
                            try:
                                self.backend.kill_window(
                                    session, op,
                                    expected_window=TmuxCommandBackend.window_identity(window))
                                after = self._verify(session)
                                present = any(w.get("window_id") == op.get("window_id")
                                              for w in after.get("windows", []))
                                op["cleanup_state"] = "pending" if present else "removed"
                            except ShellCommandError:
                                op["cleanup_state"] = "pending"
                else:
                    op["cleanup_state"] = "removed" if native.get("state") == "ready" else "unknown"
                if op.get("cleanup_state") == "removed":
                    op.setdefault("collected_at", self.clock())
            atomic_json(paths["record"], op)
            if (_layout_mode(session) == _FAMILY_LAYOUT and prune_older
                    and op.get("cleanup_state") == "retained"):
                for previous in self._strict_ops(directory, session):
                    if (previous.get("operation_id") != op.get("operation_id")
                            and previous.get("state") == "completed"
                            and previous.get("cleanup_state") != "removed"):
                        self._reconcile(directory, session, previous, prune_older=False)
            if telemetry_before != (op.get("state"), op.get("output_bytes_seen")):
                try:
                    self._refresh_telemetry(directory, session)
                except (ShellCommandError, OSError, ValueError):
                    pass
        return op

    def status(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        caller = role = slug = fingerprint = None
        if _family_like(session):
            caller, role, slug, fingerprint = self._family_caller(session, owner)
        elif owner is not None:
            self._authorize(session, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            if _family_like(session):
                caller, role, slug, fingerprint = self._family_caller(session, owner)
            elif owner is not None:
                self._authorize(session, owner)
            native = self._verify(session)
            if op_id is not None:
                op_path = self._paths(directory, op_id)["record"]
                op = read_json(op_path, limit=2_000_000)
                if _family_like(session):
                    self._authorize_operation(session, op, owner)
                op = self._reconcile(directory, session, op)
                result = self._operation_reply(session, op, directory, owner=owner)
                result.update(session_state=session.get("state"), native_state=native.get("state"))
                return result
            all_operations = sorted(self._ops(directory),
                                    key=lambda item: item[1].get("created_at", 0))
            if role == "member":
                all_operations = [(path, op) for path, op in all_operations
                                  if op.get("member_fingerprint") == fingerprint]
            recent = all_operations[-20:]
            operations = [self._operation_reply(session, self._reconcile(directory, session, op),
                                                directory, include_output=False)
                          for _, op in recent]
            if role == "member":
                session_advertisement = self._session_reply(session, created=False, joined=True)
                return _reply("ready", effect=str(session.get("effect_certainty", "known")),
                              handle=session.get("handle"), role="member",
                              member_label=f"subagent {slug}", native_state=native.get("state"),
                              machine=session_advertisement["machine"],
                              join_command=session_advertisement["join_command"],
                              operations=operations, operation_count=len(all_operations),
                              operations_truncated=len(all_operations) > len(recent))
            return self._session_reply(session, created=False, joined=True) | {
                "native_state": native.get("state"),
                "native_windows": len(native.get("windows", [])),
                "profile_readback": native.get("applied_settings"),
                "profile_settings": session.get("settings"),
                "profile_target": session.get("profile_target"),
                "profile_state": session.get("profile_state", "applied"),
                "operations": operations,
                "operation_count": len(all_operations),
                "operations_truncated": len(all_operations) > len(recent),
                "owner_liveness": owner_liveness(session.get("owner", {})),
            }

    def _doctor_known_app(self, handle: str, owner: Mapping[str, Any] | None) -> dict[str, Any] | None:
        directory, session, op_id = self._load(handle)
        if op_id is None:
            return None
        with session_lock(directory):
            session = read_json(directory / "session.json")
            operation = read_json(self._paths(directory, op_id)["record"], limit=2_000_000)
            if operation.get("controlled_app") != "less" or operation.get("state") != "running":
                return None
            app_config = _runtime_settings_for_service(session, self.source_root)["recovery"]["known_apps"]["less"]
            if not app_config["enabled"]:
                return {"action": "controlled_less_quit", "result": "preserved", "code": "disabled_by_runtime_profile"}
            if session.get("state") != "ready" or self._verify(session).get("state") != "ready":
                return {"action": "controlled_less_quit", "result": "preserved", "code": "native_unqualified"}
            lease = operation.get("input_lease")
            if (isinstance(lease, dict) and not lease.get("revoked")
                    and lease.get("machine_generation") == session.get("machine_generation")
                    and time.monotonic() < lease.get("deadline_monotonic", 0)):
                return {"action": "controlled_less_quit", "result": "preserved", "code": "human_input_lease_active"}
            reason = self._qualified_less(operation)
            if reason is not None:
                return {"action": "controlled_less_quit", "result": "preserved", "code": reason}
            input_id = "doctor-less-" + _digest({"operation": op_id, "child": operation.get("child_starttime"),
                                                  "request": operation.get("request_digest")})[:24]
            input_dir = self._paths(directory, op_id)["inputs"]
            prior = input_dir / f"{input_id}.json"
            if prior.exists():
                receipt = read_json(prior, limit=4096)
                return {"action": "controlled_less_quit", "result": "no_replay",
                        "input_state": receipt.get("state", "unknown")}
            if any(input_dir.glob("*.json")):
                return {"action": "controlled_less_quit", "result": "preserved", "code": "prior_input_unknown"}
        try:
            sent = self.input(handle, input_id, b"q", owner=owner, recovery_app="less")
        except ShellCommandError as exc:
            return {"action": "controlled_less_quit", "result": "preserved", "code": exc.code}
        try:
            observed = self.wait(handle, timeout=app_config["observe_seconds"], owner=owner)
            state = observed.get("state")
        except ShellCommandError:
            state = "unconfirmed"
        return {"action": "controlled_less_quit", "result": "sent_once",
                "input_state": sent.get("state"), "observed_state": state}

    def _repair_native_layout_locked(self, directory: Path, session: dict[str, Any]) -> dict[str, Any]:
        """Authorize a fresh geometry repair from a complete observed inventory.

        This is explicit doctor repair, never automatic replay of a lost queue
        and never a workload dispatch. Unknown occupants or protected input keep
        the original intent and all panes unchanged.
        """
        state = _validate_native_layout(session.get("native_layout"))
        if state.get("layout_state") not in {"applying", "dispatching", "unknown"}:
            return {"action": "repair_native_layout", "result": "not_needed"}
        native = self._verify(session)
        if native.get("state") != "ready":
            raise ShellCommandError("native_inventory_unknown", "Native layout repair requires exact pane identities.")
        controls = native.get("controls", {})
        if any(controls.get(key) for key in ("freeze_layout", "copy_mode", "overlay", "input_active", "zoomed")):
            raise ShellCommandError("native_layout_protected", "Protected terminal input prevents layout repair.")
        operations = self._strict_ops(directory, session)
        if any(item.get("state") in {"preparing", "unknown"}
               or item.get("effect_certainty") != "known" for item in operations):
            raise ShellCommandError("operation_unknown", "An uncertain workload prevents layout repair.")
        inventory = self._native_inventory(session, native, operations, allow_active=True)
        prior_generation = state["desired_generation"]
        session["native_layout_repair"] = {
            "prior_generation": prior_generation,
            "prior_plan_digest": state.get("requested_plan_digest"),
            "observed_inventory_digest": _digest(inventory),
            "started_at": self.clock(),
        }
        state.update(desired_generation=prior_generation + 1, layout_state="desired",
                     pending=True, activity_badge=True, layout_error=None,
                     requested_plan=None, requested_plan_digest=None)
        session["native_layout"] = state
        atomic_json(directory / "session.json", session)
        result = self._apply_native_layout_locked(directory, session, force=True)
        return {"action": "repair_native_layout", "result": result["state"],
                "desired_generation": result.get("desired_generation"),
                "applied_generation": result.get("applied_generation")}

    def doctor(self, handle: str, *, owner: Mapping[str, Any] | None = None,
               repair: bool = False) -> dict[str, Any]:
        """Attempt only proven idle repair; preserve uncertain foreground work."""
        directory, session, op_id = self._load(handle)
        if repair:
            self._authorize(session, owner)
        elif _family_like(session):
            _, role, _, fingerprint = self._family_caller(session, owner)
        elif owner is not None:
            self._authorize(session, owner)
        app_attempt = self._doctor_known_app(handle, owner) if repair else None
        with session_lock(directory, blocking=repair):
            session = read_json(directory / "session.json")
            native = self._verify(session)
            operations = self._strict_ops(directory, session)
            if not repair and _family_like(session) and role == "member":
                operations = [item for item in operations if item.get("member_fingerprint") == fingerprint]
                if op_id is not None and not any(item.get("operation_id") == op_id for item in operations):
                    raise ShellCommandError("operation_member_mismatch", "A family member can diagnose only its own operations.")
            pins, pin_unknown = self._pins(directory, remove_dead=False)
            attempts: list[dict[str, Any]] = [app_attempt] if app_attempt is not None else []
            selected = [item for item in operations if op_id is None or item.get("operation_id") == op_id]
            if repair and not pin_unknown and not pins and native.get("state") == "ready":
                for item in selected[-8:]:
                    if (item.get("state") in {"completed", "failed", "draining"}
                            and item.get("process_scope") == "collected"
                            and item.get("cleanup_state") != "removed"):
                        try:
                            updated = self._reconcile(directory, session, item)
                            attempts.append({"action": "reconcile_exact_pane", "operation_id": item["operation_id"],
                                             "result": updated.get("cleanup_state", "pending")})
                        except ShellCommandError as exc:
                            attempts.append({"action": "reconcile_exact_pane", "operation_id": item["operation_id"],
                                             "result": "preserved", "code": exc.code})
                operations = self._strict_ops(directory, session)
                selected = [item for item in operations if op_id is None or item.get("operation_id") == op_id]
            active = [item for item in selected if item.get("state") not in {"completed", "failed", "collected"}]
            uncertain = [item for item in selected if item.get("state") == "unknown"
                         or item.get("effect_certainty") == "unknown"]
            if repair and _layout_mode(session) == _NATIVE_CANVAS_LAYOUT and not pin_unknown:
                try:
                    attempts.append(self._repair_native_layout_locked(directory, session))
                    native = self._verify(session)
                except ShellCommandError as exc:
                    attempts.append({"action": "repair_native_layout",
                                     "result": "unknown" if exc.effect == "unknown" else "preserved",
                                     "code": exc.code})
            if (repair and native.get("state") == "ready"
                    and callable(getattr(self.backend, "label_completed_pane", None))):
                for item in selected[-8:]:
                    if (item.get("state") != "completed" or item.get("process_scope") != "collected"
                            or item.get("output_drain") not in {"available", "unavailable"}):
                        continue
                    matches = self._matching_operation_panes(native, session, item)
                    if len(matches) != 1 or matches[0][1].get("dead") is not True:
                        continue
                    try:
                        changed_label = self.backend.label_completed_pane(
                            session, item, expected_window=TmuxCommandBackend.window_identity(matches[0][0]))
                        attempts.append({"action": "label_exact_completed_pane", "operation_id": item["operation_id"],
                                         "result": "verified" if changed_label else "already_labeled"})
                    except ShellCommandError as exc:
                        attempts.append({"action": "label_exact_completed_pane", "operation_id": item["operation_id"],
                                         "result": "preserved", "code": exc.code})
            if (repair and native.get("state") == "ready" and not active and not uncertain
                    and not pin_unknown and callable(getattr(self.backend, "repair_idle_overview_label", None))):
                try:
                    inventory = self._native_inventory(session, native, operations)
                    changed = self.backend.repair_idle_overview_label(
                        session, expected_inventory=inventory)
                    attempts.append({"action": "label_exact_idle_overview",
                                     "result": "verified" if changed else "already_idle"})
                except ShellCommandError as exc:
                    attempts.append({"action": "label_exact_idle_overview",
                                     "result": "preserved", "code": exc.code})
            if (repair and _layout_mode(session) == _FAMILY_LAYOUT
                    and native.get("state") == "unknown" and not active and not uncertain
                    and not pins and not pin_unknown):
                try:
                    self.backend.repair_idle_family_view(session)
                    native = self._verify(session)
                    attempts.append({"action": "restore_empty_family_view",
                                     "result": "verified" if native.get("state") == "ready" else "unqualified"})
                except ShellCommandError as exc:
                    attempts.append({"action": "restore_empty_family_view",
                                     "result": "preserved", "code": exc.code})
            if op_id is not None:
                active = [item for item in active if item.get("operation_id") == op_id]
                uncertain = [item for item in uncertain if item.get("operation_id") == op_id]
            if native.get("state") != "ready":
                classification = "native_missing" if native.get("state") == "missing" else "native_unqualified"
                actions = ["Preserve records and live processes; inspect the exact session identity before recovery."]
            elif (pin_unknown or uncertain
                  or (_layout_mode(session) == _NATIVE_CANVAS_LAYOUT
                      and session.get("native_layout", {}).get("layout_state")
                      in {"applying", "dispatching", "unknown"})):
                classification = "uncertain"
                actions = ["Preserve the exact operation and observer; reconcile its receipt without replay."]
            elif active:
                classification = "working"
                actions = ["Preserve the foreground command; no application escape is qualified without exact readiness and clean-buffer evidence."]
                if repair:
                    attempts.append({"action": "application_recovery", "result": "preserved",
                                     "code": "readiness_or_clean_buffer_unproven"})
            elif pins or native.get("session", {}).get("attached", 0):
                classification = "observed_idle"
                actions = ["Keep the observer attached; no command needs dispatch."]
            else:
                classification = "idle"
                actions = ["The owned session is idle; no repair action is needed."]
            tail = ""
            if op_id is not None and selected:
                try:
                    page = self.logs(handle, lines=_AGENT_LINES,
                                     max_bytes=_AGENT_OUTPUT_BYTES, owner=owner)
                    tail = str(page.get("output", ""))
                    tail = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", tail)
                    tail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", tail)
                    tail = tail.replace("\r\n", "\n").replace("\r", "\n")
                    tail = re.sub(r"(?i)\b(password|token|secret|api[_-]?key)\s*[:=]\s*\S+",
                                  r"\1=<redacted>", tail)
                    tail = "".join(char if char in "\n\t" or ord(char) >= 0x20 else "?" for char in tail)
                    tail = "\n".join(tail.splitlines()[-_AGENT_LINES:])
                    tail = _decode_tail_output(tail.encode("utf-8"), _AGENT_OUTPUT_BYTES)[0]
                except (ShellCommandError, OSError):
                    tail = ""
            changed = any(item.get("result") in {"verified", "removed", "sent_once", "applied"} for item in attempts)
            return _reply("diagnosed", effect="known" if changed else "none", handle=handle,
                          classification=classification, native_state=native.get("state"),
                          reason=native.get("reason"), active_operations=len(active),
                          uncertain_operations=len(uncertain), observer_pins=len(pins),
                          observer_state="unknown" if pin_unknown else native.get("session", {}).get("attached"),
                          actions=actions, attempts=attempts,
                          tail=tail, tail_truncated=len(tail.encode("utf-8")) >= _AGENT_OUTPUT_BYTES,
                          prompt_state="not_applicable_fresh_command_pane",
                          app_recovery="manual_handoff_until_exact_readiness_and_dirty_state_are_proven",
                          handoff_required=(classification in {"native_missing", "native_unqualified", "uncertain"}
                                            or repair and bool(active)),
                          automatic_replay=False)

    def viewport(self, handle: str, *, height: int, width: int | None = None,
                 owner: Mapping[str, Any] | None = None,
                 _observer_pin: tuple[str, int] | None = None) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None or not _family_like(session):
            raise ShellCommandError("family_session_required", "Viewport sizing requires a family session handle.")
        native_canvas = _layout_mode(session) == _NATIVE_CANVAS_LAYOUT
        canvas = _layout_mode(session) == _CANVAS_LAYOUT
        if _observer_pin is None:
            self._authorize(session, owner)
        else:
            self._human_terminal_for(session)
        if type(height) is not int or not 1 <= height <= (512 if canvas or native_canvas else _family_max_rows(session["settings"])):
            raise ShellCommandError("invalid_viewport", "Family height exceeds its history-safe row limit.")
        if width is not None and (not (canvas or native_canvas) or type(width) is not int
                                  or not (80 <= width <= 500 if native_canvas else 1 <= width <= 500)):
            raise ShellCommandError("invalid_viewport", "Canvas width is outside the supported observed column range.")
        with session_lock(directory, blocking=True):
            session = read_json(directory / "session.json")
            if _observer_pin is None:
                self._authorize(session, owner)
            else:
                self._human_terminal_for(session)
                pin_id, viewer_pid = _observer_pin
                pin_path = directory / "pins" / f"attach-{pin_id}.json"
                pin = read_json(pin_path, limit=4096) if pin_path.exists() and not pin_path.is_symlink() else {}
                viewer = process_info(viewer_pid)
                current = process_info(os.getpid())
                clients = getattr(self.backend, "_clients", lambda _session: [])(session)
                if (pin.get("pin_id") != pin_id or pin.get("kind") != "attachment"
                        or pin.get("primary_viewport") is not True
                        or current is None or pin.get("pid") != os.getpid()
                        or pin.get("starttime") != current["starttime"]
                        or viewer is None or viewer.get("ppid") != os.getpid()
                        or len([client for client in clients if client.get("pid") == viewer_pid
                                and "ignore-size" in client.get("flags", set())]) != 1):
                    raise ShellCommandError("observer_viewport_unknown", "The primary viewer no longer owns this viewport.", effect="unknown")
            self._require_family_root_live(session)
            native = self._verify(session)
            if native.get("state") != "ready":
                raise ShellCommandError("session_identity_unknown", "The family window is not qualified for resize.", effect="unknown")
            operations = self._strict_ops(directory, session)
            self._native_inventory(session, native, operations, allow_active=True)
            if native_canvas:
                from .command_viewport import set_viewport
                state = _validate_native_layout(session.get("native_layout"))
                target_width = width if width is not None else state["columns"]
                if height == state["rows"] and target_width == state["columns"]:
                    return _reply("unchanged", effect="known", handle=handle,
                                  width=target_width, height=height,
                                  desired_generation=state["desired_generation"],
                                  applied_generation=state["applied_generation"])
                session["native_layout"] = state
                set_viewport(session, columns=target_width, rows=height)
                atomic_json(directory / "session.json", session)
                return _reply("pending", effect="known", handle=handle,
                              width=target_width, height=height,
                              observer_width=width,
                              desired_generation=state["desired_generation"],
                              applied_generation=state["applied_generation"],
                              activity_badge=True)
            window = next(item for item in native["windows"]
                          if item["window_id"] == session["status_window_id"])
            if not canvas:
                operation_count = len(window["panes"]) - 1
                minimum = 1 if operation_count == 0 else (
                    (operation_count + 1) * _FAMILY_MIN_PANE_ROWS + operation_count)
                if height < minimum:
                    raise ShellCommandError("family_capacity", "The requested height would hide a live family pane.")
            target_width = (max(80, width) if width is not None else session.get("canvas_width")) if canvas else _runtime_settings_for_service(session, self.source_root)["layout"]["canvas_columns"]
            warning = canvas and width is not None and width < 80
            if (height == session.get("primary_viewport_height")
                    and target_width == (session.get("canvas_width") if canvas else target_width)
                    and (not canvas or warning == session.get("canvas_width_warning", False))):
                return _reply("unchanged", effect="known", handle=handle, width=target_width, height=height)
            session["viewport_intent"] = {"height": height, "width": target_width,
                                           "started_at": self.clock()}
            atomic_json(directory / "session.json", session)
            try:
                resized = self.backend.resize_family_window(
                    session, expected_window=TmuxCommandBackend.window_identity(window), height=height,
                    **({"width": target_width} if canvas else {}))
                session["primary_viewport_height"] = height
                if canvas:
                    session["canvas_width"] = target_width
                    session["canvas_width_warning"] = warning
                session["viewport_intent"] = None
                atomic_json(directory / "session.json", session)
                if not canvas:
                    self.backend.arrange_family_window(
                        session, expected_window=TmuxCommandBackend.window_identity(resized))
                after = self._verify(session)
                self._native_inventory(session, after, operations, allow_active=True)
                return _reply("resized", effect="known", handle=handle, width=target_width, height=height,
                              observer_width=width if canvas else None)
            except ShellCommandError as exc:
                session["viewport_state"] = "unknown"
                atomic_json(directory / "session.json", session)
                raise ShellCommandError(exc.code, exc.message, effect="unknown") from exc

    def wait(
        self, handle: str, *, timeout: float = 60.0,
        owner: Mapping[str, Any] | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Observe one operation until a terminal receipt or bounded timeout."""
        _, _, op_id = self._load(handle)
        if op_id is None:
            raise ShellCommandError("operation_handle_required", "Waiting requires an operation handle.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 <= timeout <= 86_400:
            raise ShellCommandError("invalid_timeout", "Wait timeout must be from 0 through 86400 seconds.")
        deadline = time.monotonic() + float(timeout)
        last_progress = time.monotonic()
        while True:
            try:
                observed = self.status(handle, owner=owner)
            except ShellCommandError as exc:
                if exc.code != "busy":
                    raise
                observed = _reply("busy", ok=False, handle=handle, wait_timed_out=False)
            if observed.get("state") in {"completed", "failed", "collected", "unknown"}:
                return observed | {"wait_timed_out": False}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return observed | {"wait_timed_out": True}
            if progress is not None and time.monotonic() - last_progress >= 300:
                progress({"api_version": 1, "event": "join_reminder",
                          "machine": observed.get("machine"),
                          "join_command": observed.get("join_command")})
                last_progress = time.monotonic()
            time.sleep(min(0.1, remaining))

    def screen(
        self, handle: str, *, lines: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        operation = read_json(self._paths(directory, op_id)["record"], limit=2_000_000) if op_id else None
        if _family_like(session):
            caller, role, _slug, _fingerprint = self._family_caller(session, owner)
            if operation is not None:
                self._authorize_operation(session, operation, owner)
            elif role != "root":
                raise ShellCommandError("operation_handle_required", "A family member can view only its own operation pane.")
        elif owner is not None:
            self._authorize(session, owner)
        count = session["settings"]["default_lines"] if lines is None else lines
        if type(count) is not int or not 1 <= count <= 10_000:
            raise ShellCommandError("invalid_lines", "Screen view lines must be from 1 through 10000.")
        count = min(count, _AGENT_LINES)
        native = self._verify(session)
        if native.get("state") != "ready":
            raise ShellCommandError("session_identity_unknown", "The exact owned tmux session could not be verified.", effect="unknown")
        if _layout_mode(session) == _CANVAS_LAYOUT and operation is not None:
            observed = self.logs(handle, lines=count, owner=owner)
            screen = str(observed.get("output", ""))
        else:
            screen = self.backend.capture_screen(session, operation, lines=count)
        raw = screen.encode("utf-8", errors="replace")
        limit = min(session["settings"]["max_response_bytes"], _AGENT_OUTPUT_BYTES)
        screen, truncated = _decode_tail_output(raw, limit)
        return _reply("observed", handle=handle, view_kind="screen",
                      lines=screen.splitlines()[-count:], truncated=truncated)

    def view(
        self, handle: str, *, lines: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Show recent captured output; screen() shows the pane's current rendering."""
        directory, session, op_id = self._load(handle)
        role = "root"
        fingerprint = None
        if _family_like(session):
            _caller, role, _slug, fingerprint = self._family_caller(session, owner)
        if op_id is None:
            operations = self._ops(directory)
            if role == "member":
                operations = [(path, op) for path, op in operations
                              if op.get("member_fingerprint") == fingerprint]
            if not operations:
                return _reply("observed", handle=handle, view_kind="recent_output",
                              lines=[], truncated=False, operation_handle=None)
            op_id = str(max(operations, key=lambda item: item[1].get("created_at", 0))[1]["operation_id"])
        op_handle = self._op_handle(str(session["scope_key"]), str(session["session_id"]), op_id)
        observed = self.logs(op_handle, lines=lines, owner=owner)
        return _reply("observed", handle=handle, view_kind="recent_output",
                      operation_handle=op_handle, lines=observed["output"].splitlines(),
                      truncated=observed["truncated"], base_cursor=observed["base_cursor"],
                      end_cursor=observed["end_cursor"])

    def logs(
        self, handle: str, *, cursor: int | None = None,
        before_cursor: int | None = None,
        lines: int | None = None, max_bytes: int | None = None,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        directory, session, op, _ = self._load_operation(handle)
        if _family_like(session):
            self._authorize_operation(session, op, owner)
        paths = self._paths(directory, str(op["operation_id"]))
        settings = session["settings"]
        limit = min(settings["max_response_bytes"], _AGENT_OUTPUT_BYTES) if max_bytes is None else max_bytes
        if type(limit) is not int or not 1 <= limit <= min(settings["max_response_bytes"], _AGENT_OUTPUT_BYTES):
            raise ShellCommandError("invalid_max_bytes", "Log output is limited to 32768 bytes per response.")
        line_count = settings["default_lines"] if lines is None else lines
        if type(line_count) is not int or not 1 <= line_count <= 10_000:
            raise ShellCommandError("invalid_lines", "Log line count must be from 1 through 10000.")
        line_count = min(line_count, _AGENT_LINES)
        with log_lock(paths["cursor"]):
            meta = read_json(paths["cursor"], limit=4096) if paths["cursor"].exists() else {
                "base_offset": 0, "end_offset": 0, "available": False,
            }
            base, end = meta.get("base_offset", 0), meta.get("end_offset", 0)
            if type(base) is not int or type(end) is not int or base < 0 or end < base:
                base, end = 0, 0
            storage_available = (bool(meta.get("available"))
                                 and op.get("capture_storage") != "unavailable"
                                 and op.get("output_drain") != "unavailable")
            if cursor is not None and (type(cursor) is not int or cursor < 0):
                raise ShellCommandError("invalid_cursor", "The log cursor must be a non-negative integer.")
            if before_cursor is not None and (cursor is not None or type(before_cursor) is not int or before_cursor < 0):
                raise ShellCommandError("invalid_cursor", "Use one non-negative forward or previous-page cursor.")
            if _layout_mode(session) == _CANVAS_LAYOUT and not storage_available:
                return _reply(str(op.get("state", "unknown")), effect=str(op.get("effect_certainty", "unknown")),
                              handle=handle, operation_handle=op.get("operation_handle", handle),
                              operation_id=op["operation_id"], output="", cursor=end,
                              base_cursor=base, end_cursor=end, gap=cursor is not None and cursor < base,
                              page_start_cursor=end, previous_cursor=None, next_cursor=end,
                              truncated=False, storage=False,
                              output_drain=op.get("output_drain", "pending"))
            if before_cursor is not None:
                boundary = min(end, max(base, before_cursor))
                start = max(base, boundary - limit)
                if paths["log"].exists():
                    with paths["log"].open("rb") as stream:
                        stream.seek(start - base)
                        data = stream.read(boundary - start)
                else:
                    data = b""
                parts = data.splitlines(keepends=True)
                selected = b"".join(parts[-line_count:])
                start += len(data) - len(selected)
                output, skipped = _decode_tail_page(selected, limit)
                page_start = start + skipped
                return _reply(str(op.get("state", "unknown")),
                              effect=str(op.get("effect_certainty", "unknown")),
                              handle=handle, operation_handle=op.get("operation_handle", handle),
                              operation_id=op["operation_id"], output=output,
                              cursor=boundary, page_start_cursor=page_start,
                              previous_cursor=page_start if page_start > base else None,
                              next_cursor=boundary, base_cursor=base, end_cursor=end,
                              gap=before_cursor < base or before_cursor > end,
                              truncated=page_start > base or boundary < end,
                              storage=storage_available,
                              output_drain=op.get("output_drain", "pending"))
            gap = cursor is not None and cursor < base
            if not paths["log"].exists():
                start, data = max(base, cursor if cursor is not None else base), b""
            elif cursor is not None:
                start = max(base, cursor)
                with paths["log"].open("rb") as stream:
                    stream.seek(start - base)
                    data = stream.read(limit)[:limit]
                data = b"".join(data.splitlines(keepends=True)[:line_count])
            else:
                collected = bytearray()
                position = paths["log"].stat().st_size
                newline_count = 0
                with paths["log"].open("rb") as stream:
                    while position and len(collected) < limit and newline_count <= line_count:
                        size = min(64 * 1024, position, limit - len(collected))
                        position -= size
                        stream.seek(position)
                        block = stream.read(size)
                        collected[:0] = block
                        newline_count += block.count(b"\n")
                parts = bytes(collected).splitlines(keepends=True)
                data = b"".join(parts[-line_count:])
                start = base + max(0, paths["log"].stat().st_size - len(data))
            if cursor is None:
                output, skipped = _decode_tail_page(data, limit)
                start += skipped
                decode_truncated = skipped > 0
                consumed = len(data) - skipped
            else:
                output, consumed = _decode_cursor_output(
                    data, limit,
                    final=op.get("output_drain") == "available" and start + len(data) >= end,
                )
                decode_truncated = consumed < len(data)
            next_cursor = start + consumed
            tail_omitted = cursor is None and start > base
            return _reply(
                str(op.get("state", "unknown")), effect=str(op.get("effect_certainty", "unknown")),
                handle=handle, operation_handle=op.get("operation_handle", handle),
                operation_id=op["operation_id"], output=output,
                cursor=next_cursor, base_cursor=base, end_cursor=end, gap=gap,
                page_start_cursor=start,
                previous_cursor=start if start > base else None,
                next_cursor=next_cursor,
                truncated=decode_truncated or tail_omitted or next_cursor < end,
                storage=storage_available,
                output_drain=op.get("output_drain", "pending"),
            )

    @staticmethod
    def _qualified_less(operation: Mapping[str, Any]) -> str | None:
        if operation.get("controlled_app") != "less":
            return "controlled_launch_missing"
        argv = operation.get("controlled_argv")
        if (not isinstance(argv, list) or len(argv) != 4 or argv[1:3] != ["-K", "-L"]
                or not isinstance(argv[0], str) or not Path(argv[0]).is_absolute()):
            return "controlled_argv_unknown"
        runner_pid, child_pid = operation.get("runner_pid"), operation.get("child_pid")
        if type(runner_pid) is not int or type(child_pid) is not int:
            return "process_identity_unknown"
        runner, child = process_info(runner_pid), process_info(child_pid)
        if (runner is None or child is None or runner.get("starttime") != operation.get("runner_starttime")
                or child.get("starttime") != operation.get("child_starttime")
                or child.get("ppid") != runner_pid or child.get("pgrp") != operation.get("child_pgrp")
                or child.get("tpgid") != child.get("pgrp") or child.get("tty_nr") == 0
                or process_state(child_pid) in {None, "Z", "X"}):
            return "foreground_identity_unknown"
        try:
            command = Path(f"/proc/{child_pid}/cmdline").read_bytes().split(b"\0")
            executable = Path(f"/proc/{child_pid}/exe").resolve(strict=True)
            children = Path(f"/proc/{child_pid}/task/{child_pid}/children").read_text().strip()
            runner_children = Path(f"/proc/{runner_pid}/task/{runner_pid}/children").read_text().strip()
        except OSError:
            return "process_inventory_unknown"
        if (executable != Path(argv[0]).resolve(strict=False)
                or command[-1:] != [b""] or [os.fsdecode(value) for value in command[:-1]] != argv
                or children or runner_children.split() != [str(child_pid)]):
            return "application_or_child_inventory_changed"
        return None

    def input(
        self, handle: str, input_id: str, data: bytes | str, *,
        eof: bool = False, owner: Mapping[str, Any] | None = None,
        lease_id: str | None = None, grantee: str | None = None,
        recovery_app: str | None = None,
    ) -> dict[str, Any]:
        input_id = validate_identifier(input_id, "input_id", maximum=128)
        raw = data.encode() if isinstance(data, str) else data
        if not isinstance(raw, bytes) or len(raw) > 1_048_576 or type(eof) is not bool:
            raise ShellCommandError("invalid_input", "Interactive input must be bounded text or bytes with a Boolean EOF.")
        directory, session, op, op_path = self._load_operation(handle)
        human_lease = owner is None and lease_id is not None
        if human_lease:
            self._human_terminal_for(session)
        else:
            self._authorize_operation(session, op, owner)
        digest = _digest({"data_b64": base64.b64encode(raw).decode("ascii"), "eof": eof})
        with session_lock(directory, blocking=True):
            session = read_json(directory / "session.json")
            if session.get("state") != "ready":
                raise ShellCommandError("session_not_ready", "This session is closed for input.")
            self._require_family_root_live(session)
            paths = self._paths(directory, str(op["operation_id"]))
            current = read_json(op_path, limit=2_000_000)
            if human_lease:
                self._human_terminal_for(session)
            else:
                self._authorize_operation(session, current, owner)
            if lease_id is not None:
                lease = current.get("input_lease")
                if (not isinstance(lease, dict) or lease.get("id") != lease_id
                        or lease.get("grantee") != grantee or lease.get("revoked")
                        or lease.get("machine_generation") != session.get("machine_generation")
                        or type(lease.get("deadline_monotonic")) not in {int, float}
                        or time.monotonic() >= lease["deadline_monotonic"]
                        or any(lease.get(key) != current.get(key) for key in
                               ("pane_id", "runner_pid", "runner_starttime", "request_digest"))):
                    raise ShellCommandError("input_lease_expired", "The exact mediated input lease is unavailable or expired.")
            input_dir = paths["inputs"]
            input_dir.mkdir(mode=0o700, exist_ok=True)
            item_path = input_dir / f"{input_id}.json"
            if item_path.exists():
                previous = read_json(item_path, limit=4096)
                if previous.get("request_digest") != digest:
                    raise ShellCommandError("input_id_conflict", "This input ID already names different bytes.")
                return _reply(previous.get("state", "sent"), effect=previous.get("effect_certainty", "known"),
                              handle=handle, input_id=input_id, repeated=True)
            if recovery_app is not None:
                if recovery_app != "less" or raw != b"q" or eof or lease_id is not None:
                    raise ShellCommandError("recovery_input_invalid", "Only the exact controlled pager quit is supported.")
                lease = current.get("input_lease")
                if (isinstance(lease, dict) and not lease.get("revoked")
                        and lease.get("machine_generation") == session.get("machine_generation")
                        and time.monotonic() < lease.get("deadline_monotonic", 0)):
                    raise ShellCommandError("human_input_lease_active", "A requested input lease protects the pager.")
                reason = self._qualified_less(current)
                if reason is not None:
                    raise ShellCommandError(reason, "The controlled pager foreground could not be proven.")
                if any(path.name != f"{input_id}.json" for path in input_dir.glob("*.json")):
                    raise ShellCommandError("prior_input_unknown", "Earlier pager input may have changed its command mode.")
            if current.get("state") != "running" or current.get("process_scope") not in {"tracking", None}:
                raise ShellCommandError("operation_not_interactive", "The exact command is not an active PTY operation.")
            runner = process_info(current.get("runner_pid")) if type(current.get("runner_pid")) is int else None
            child = process_info(current.get("child_pid")) if type(current.get("child_pid")) is int else None
            runner_state = process_state(int(current["runner_pid"])) if type(current.get("runner_pid")) is int else None
            child_state = process_state(int(current["child_pid"])) if type(current.get("child_pid")) is int else None
            if (runner is None or runner.get("starttime") != current.get("runner_starttime")
                    or runner_state is None or runner_state in {"Z", "X"}
                    or child is None or child.get("starttime") != current.get("child_starttime")
                    or child_state is None or child_state in {"Z", "X"}
                    or child.get("pgrp") != current.get("child_pgrp")):
                raise ShellCommandError("operation_identity_unknown", "The active command process identity cannot be verified.", effect="unknown")
            native = self._verify(session)
            if _family_like(session):
                attached = native.get("session", {}).get("attached")
                pins, pin_unknown = self._pins(directory)
                if type(attached) is not int or pin_unknown or len(pins) > attached:
                    raise ShellCommandError("observer_identity_unknown", "The attached viewers are not qualified for mediated input.", effect="unknown")
            if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT}:
                self._native_inventory(session, native, self._strict_ops(directory, session), allow_active=True)
            else:
                panes = [pane for _window, pane in self._matching_operation_panes(native, session, current)]
                if len(panes) != 1 or panes[0].get("dead") or panes[0].get("pane_pid") != current.get("runner_pid"):
                    raise ShellCommandError("operation_identity_unknown", "The exact active command pane cannot be verified.", effect="unknown")
            item = {"schema_version": 1, "input_id": input_id, "request_digest": digest,
                    "state": "intent", "effect_certainty": "unknown", "eof": eof, "created_at": self.clock()}
            atomic_json(item_path, item, exclusive=True)
            input_file = input_dir / f"{input_id}.input"
            fd = os.open(input_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if _layout_mode(session) == _CANVAS_LAYOUT:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        receipt = read_json(item_path, limit=4096)
                        if receipt.get("state") == "sent" and receipt.get("request_digest") == digest:
                            current_session = read_json(directory / "session.json")
                            current_session.update(last_activity_at=self.clock(), last_activity_kind="input")
                            if _layout_mode(current_session) == _NATIVE_CANVAS_LAYOUT:
                                current_session.update(follow_event_operation_id=current.get("operation_id"),
                                                       follow_event_monotonic=time.monotonic())
                            atomic_json(directory / "session.json", current_session)
                            return _reply("sent", effect="known", handle=handle, input_id=input_id, repeated=False)
                        if receipt.get("state") not in {"intent", "dispatching"}:
                            break
                        time.sleep(.01)
                    raise ShellCommandError("pty_input_ack_unknown", "The runner did not confirm this exact input; it was not replayed.", effect="unknown")
                self.backend.send_input(session, current, buffer_name=f"ls-{input_id[:80]}",
                                        input_path=str(input_file), eof=eof)
                item.update(state="sent", effect_certainty="known", sent_at=self.clock())
                atomic_json(item_path, item)
                current_session = read_json(directory / "session.json")
                current_session.update(last_activity_at=self.clock(), last_activity_kind="input")
                if _layout_mode(current_session) == _NATIVE_CANVAS_LAYOUT:
                    current_session.update(follow_event_operation_id=current.get("operation_id"),
                                           follow_event_monotonic=time.monotonic())
                atomic_json(directory / "session.json", current_session)
                input_file.unlink(missing_ok=True)
                return _reply("sent", effect="known", handle=handle, input_id=input_id, repeated=False)
            except ShellCommandError as exc:
                if _layout_mode(session) == _CANVAS_LAYOUT and exc.code == "pty_input_ack_unknown":
                    # The live runner may still be draining a partial nonblocking
                    # write. Its dispatching receipt is the only authority that
                    # can advance to sent; the caller must never replay it.
                    raise
                item.update(state="unknown", effect_certainty="unknown", error=exc.code)
                atomic_json(item_path, item)
                raise ShellCommandError(exc.code, "PTY input acknowledgement is uncertain; bytes were not replayed.", effect="unknown") from exc

    def input_lease(
        self, handle: str, *, grantee: str, ttl: int = 300, revoke: bool = False,
        owner: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        grantee = validate_identifier(grantee, "grantee", maximum=64)
        if type(ttl) is not int or not 1 <= ttl <= 600:
            raise ShellCommandError("invalid_lease_ttl", "Input lease TTL must be from 1 through 600 seconds.")
        directory, session, operation, op_path = self._load_operation(handle)
        self._authorize_operation(session, operation, owner)
        with session_lock(directory, blocking=True):
            session = read_json(directory / "session.json")
            if session.get("state") != "ready" and not revoke:
                raise ShellCommandError("session_not_ready", "This session is closed for input requests.")
            if not revoke:
                self._require_family_root_live(session)
            operation = read_json(op_path, limit=2_000_000)
            self._authorize_operation(session, operation, owner)
            if revoke:
                lease = operation.get("input_lease")
                if isinstance(lease, dict):
                    lease["revoked"] = True
                    operation["input_lease"] = lease
                    atomic_json(op_path, operation)
                self._refresh_telemetry(directory, session)
                return _reply("revoked", effect="known", handle=handle,
                              native_observer_input="disabled")
            if operation.get("state") != "running" or operation.get("process_scope") != "tracking":
                raise ShellCommandError("operation_not_interactive", "Only a verified running pane can receive an input request.")
            native = self._verify(session)
            if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT}:
                self._native_inventory(session, native, self._strict_ops(directory, session), allow_active=True)
            else:
                matches = self._matching_operation_panes(native, session, operation)
                if len(matches) != 1 or matches[0][1].get("dead"):
                    raise ShellCommandError("operation_identity_unknown", "The requested input pane is not verified.", effect="unknown")
            lease = {"id": uuid.uuid4().hex, "grantee": grantee, "created_at": self.clock(),
                     "expires_at": self.clock() + ttl, "deadline_monotonic": time.monotonic() + ttl,
                     "machine_generation": session["machine_generation"],
                     "pane_id": operation.get("pane_id"), "runner_pid": operation.get("runner_pid"),
                     "runner_starttime": operation.get("runner_starttime"),
                     "request_digest": operation.get("request_digest"), "revoked": False}
            operation["input_lease"] = lease
            if _layout_mode(session) == _CANVAS_LAYOUT:
                session["activation_sequence"] = int(session.get("activation_sequence", 0)) + 1
                operation["activation_seq"] = session["activation_sequence"]
                operation["activated_monotonic"] = time.monotonic()
                atomic_json(directory / "session.json", session)
            elif _layout_mode(session) == _NATIVE_CANVAS_LAYOUT:
                session.update(follow_event_operation_id=operation.get("operation_id"),
                               follow_event_monotonic=time.monotonic(),
                               last_activity_at=self.clock(), last_activity_kind="input_request")
                atomic_json(directory / "session.json", session)
            atomic_json(op_path, operation)
            self._refresh_telemetry(directory, session)
            return _reply("requested", effect="known", handle=handle, lease_id=lease["id"],
                          grantee=grantee, expires_at=lease["expires_at"],
                          request_digest=lease["request_digest"], pane_id=lease["pane_id"],
                          runner_pid=lease["runner_pid"],
                          native_observer_input="disabled",
                          input_command=self._shell_command("input", "--handle", handle,
                                                            "--lease", lease["id"], "--grantee", grantee)
                          + " --id <input-id> --data <text>")

    def cancel(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op, op_path = self._load_operation(handle)
        self._require_family_root_live(session)
        self._authorize_operation(session, op, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            self._require_family_root_live(session)
            current = read_json(op_path, limit=2_000_000)
            self._authorize_operation(session, current, owner)
            if current.get("state") in {"completed", "failed", "collected"}:
                return self._operation_reply(session, current, directory, owner=owner) | {"cancel_requested": False}
            if current.get("state") == "draining" and current.get("process_scope") == "collected":
                return self._operation_reply(session, current, directory, owner=owner) | {
                    "cancel_requested": False, "cancel_state": "process_scope_collected"}
            if current.get("state") not in {"running", "draining"} or current.get("process_scope") not in {"tracking", "draining"}:
                raise ShellCommandError("operation_not_cancellable", "Only a verified active command can be cancelled.",
                                        effect="unknown" if current.get("state") == "unknown" else "none")
            native = self._verify(session)
            panes = ([pane for _window, pane in self._matching_operation_panes(native, session, current)]
                     if _layout_mode(session) != _CANVAS_LAYOUT else [])
            runner_pid = current.get("runner_pid")
            runner = process_info(runner_pid) if type(runner_pid) is int else None
            if ((not panes if _layout_mode(session) != _CANVAS_LAYOUT else native.get("state") != "ready")
                    or (panes and (panes[0].get("dead") or panes[0].get("pane_pid") != runner_pid))
                    or runner is None
                    or runner.get("starttime") != current.get("runner_starttime")
                    or process_state(runner_pid) in {None, "Z", "X"}
                    or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")):
                raise ShellCommandError("operation_identity_unknown", "The exact runner and pane cannot be verified for cancellation.", effect="unknown")
            if current.get("cancel_requested_at"):
                return self._operation_reply(session, current, directory, owner=owner) | {
                    "cancel_requested": True, "cancel_state": current.get("cancel_state", "requested")}
            current.update(cancel_state="requested", cancel_requested_at=self.clock())
            atomic_json(op_path, current)
            try:
                descriptor = os.pidfd_open(runner_pid)
                try:
                    fresh = process_info(runner_pid)
                    if fresh is None or fresh.get("starttime") != current["runner_starttime"]:
                        raise ShellCommandError("operation_identity_unknown", "The runner changed identity before cancellation.", effect="unknown")
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                finally:
                    os.close(descriptor)
            except (OSError, ShellCommandError) as exc:
                current.update(cancel_state="unknown", effect_certainty="unknown")
                atomic_json(op_path, current)
                raise ShellCommandError("cancel_signal_failed", "The verified runner did not accept cancellation.", effect="unknown") from exc
            result = self._operation_reply(session, current, directory, owner=owner)
            result.update(cancel_requested=True, cancel_state="requested")
            return result

    def _pins(self, directory: Path, *, remove_dead: bool = True) -> tuple[list[dict[str, Any]], bool]:
        found: list[dict[str, Any]] = []
        unknown = False
        pin_dir = directory / "pins"
        try:
            paths = list(pin_dir.glob("*.json"))
        except OSError:
            return [], True
        for path in paths:
            if path.is_symlink() or not path.is_file():
                unknown = True
                continue
            try:
                pin = read_json(path, limit=4096)
            except ShellCommandError:
                unknown = True
                continue
            life = owner_liveness(pin)
            if life == "live":
                found.append(pin)
            elif life == "unknown":
                unknown = True
            elif remove_dead:
                path.unlink(missing_ok=True)
        return found, unknown

    def _expire_completed_logs(self, directory: Path, session: Mapping[str, Any], now: float) -> list[str]:
        retention = int(session.get("settings", {}).get("completed_log_retention_days", 7))
        expired: list[str] = []
        for _, operation in self._ops(directory):
            if operation.get("state") not in {"completed", "failed", "collected"}:
                continue
            finished = operation.get("finished_at", operation.get("command_finished_at", operation.get("completed_at")))
            if type(finished) not in {int, float} or now - float(finished) < retention * 86_400:
                continue
            paths = self._paths(directory, str(operation.get("operation_id", "")))
            for key in ("log", "cursor"):
                try:
                    paths[key].unlink(missing_ok=True)
                except OSError:
                    continue
            operation["log_retention_state"] = "expired"
            operation["logs_expired_at"] = now
            atomic_json(paths["record"], operation)
            expired.append(str(operation.get("operation_id")))
        return expired

    def attach(
        self, handle: str, *, owner: Mapping[str, Any] | None = None,
        human_observer: bool = False,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        pin_dir = directory / "pins"
        pin_id = uuid.uuid4().hex
        pin_path = pin_dir / f"attach-{pin_id}.json"
        current = make_owner_binding(owner_pid=os.getpid())
        pin = {**current, "pin_id": pin_id, "kind": "attachment", "created_at": self.clock()}
        operation = None
        primary = False
        viewer_pid: int | None = None
        with session_lock(directory, blocking=True):
            session = read_json(directory / "session.json")
            if session.get("state") == "retiring":
                raise ShellCommandError("session_retiring", "This session is closed for new observers.")
            family_role = "root"
            if _family_like(session):
                if human_observer:
                    if owner is not None or op_id is not None:
                        raise ShellCommandError("observer_session_required", "A human observer must use an exact session alias.")
                    self._human_terminal_for(session)
                    family_role = "observer"
                else:
                    _caller, family_role, _slug, _fingerprint = self._family_caller(session, owner)
                if op_id is None and family_role != "root":
                    if family_role != "observer":
                        raise ShellCommandError("operation_handle_required", "A family member can attach only to its own operation pane.")
            if op_id is not None:
                operation = read_json(self._paths(directory, op_id)["record"], limit=2_000_000)
                if (operation.get("operation_id") != op_id
                        or operation.get("session_handle") != session.get("handle")):
                    raise ShellCommandError("operation_identity_mismatch", "The operation handle does not match private state.", effect="unknown")
                if _family_like(session):
                    self._authorize_operation(session, operation, owner)
                    if family_role == "member":
                        raise ShellCommandError("family_member_attach_unavailable", "A shared family window could expose sibling panes; members may inspect their own pane through screen and logs.")
                if (operation.get("state") in {"completed", "failed", "collected"}
                        or operation.get("process_scope") == "collected"):
                    return self._operation_reply(session, operation, directory, owner=owner) | {
                        "attached": False,
                        "result": {"state": operation.get("state"), "exit_code": operation.get("exit_code")},
                    }
                if operation.get("state") not in {"running", "draining"}:
                    raise ShellCommandError("operation_identity_unknown", "The operation pane is not verified for attach.", effect="unknown")
            native = self._verify(session)
            if native.get("state") != "ready":
                raise ShellCommandError("session_identity_unknown", "The exact owned tmux session cannot be verified for attach.", effect="unknown")
            if (_family_like(session) and family_role in {"root", "observer"}
                    and (op_id is None or _layout_mode(session) == _NATIVE_CANVAS_LAYOUT)):
                existing_pins, pin_unknown = self._pins(directory, remove_dead=False)
                attached = native.get("session", {}).get("attached")
                if not pin_unknown and not existing_pins and type(attached) is int and attached == 0:
                    primary = True
                    pin["primary_viewport"] = True
            if operation is not None and _layout_mode(session) != _CANVAS_LAYOUT:
                matches = self._matching_operation_panes(native, session, operation)
                if len(matches) != 1:
                    raise ShellCommandError("operation_identity_unknown", "The exact owned operation window cannot be verified for attach.", effect="unknown")
                _window, pane = matches[0]
                if (pane.get("pane_id") != operation.get("pane_id") or pane.get("dead")
                        or (operation.get("runner_pid") is not None
                            and pane.get("pane_pid") != operation.get("runner_pid"))):
                    raise ShellCommandError("operation_identity_unknown", "The exact owned operation pane cannot be verified for attach.", effect="unknown")
            atomic_json(pin_path, pin, exclusive=True)
        try:
            if _family_like(session):
                def promote(pid: int) -> None:
                    nonlocal viewer_pid
                    with session_lock(directory, blocking=True):
                        if (not pin_path.exists() or read_json(pin_path, limit=4096).get("pin_id") != pin_id
                                or process_info(pid) is None):
                            raise ShellCommandError("observer_identity_unknown", "The new viewer process is not verified.", effect="unknown")
                        current_session = read_json(directory / "session.json")
                        if self._verify(current_session).get("state") != "ready":
                            raise ShellCommandError("observer_guard_unknown", "The family pane guard changed before attachment.", effect="unknown")
                        self.backend.promote_observer(current_session, pid)
                        promoted_pin = read_json(pin_path, limit=4096)
                        promoted_pin["viewer_pid"] = pid
                        atomic_json(pin_path, promoted_pin)
                        viewer_pid = pid
            if primary and _layout_mode(session) != _NATIVE_CANVAS_LAYOUT:
                def resize(rows: int) -> None:
                    canvas_view = _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT}
                    columns = os.get_terminal_size(0).columns if canvas_view else None
                    if canvas_view and session["settings"]["status"]:
                        rows = max(1, rows - int(session["runtime_settings"]["baseline"]["status_rows"]))
                    if human_observer:
                        if viewer_pid is None:
                            raise ShellCommandError("observer_viewport_unknown", "The primary viewer is not qualified.")
                        self.viewport(str(session["handle"]), height=rows, width=columns,
                                      _observer_pin=(pin_id, viewer_pid))
                    else:
                        self.viewport(str(session["handle"]), height=rows, width=columns, owner=owner)
                returncode = self.backend.attach(session, None if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else operation, on_resize=resize,
                                                 on_ready=promote)
            elif _family_like(session):
                returncode = self.backend.attach(session, None if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else operation, on_ready=promote)
            else:
                returncode = self.backend.attach(session, operation)
            if operation is not None:
                current = read_json(self._paths(directory, op_id)["record"], limit=2_000_000)
                if (current.get("operation_id") != op_id
                        or current.get("session_handle") != session.get("handle")):
                    raise ShellCommandError("operation_identity_mismatch", "The operation changed identity during attach.", effect="unknown")
                receipt = self._operation_reply(session, current, directory,
                                                include_output=False, owner=owner)
                if returncode != 0:
                    receipt.update(join_command=receipt["session_join_command"],
                                   operation_join_command=None, join_target="session",
                                   pane_available=False)
                return receipt | {"state": "detached" if returncode == 0 else "attach_failed",
                                  "ok": returncode == 0,
                                  "attached": returncode == 0, "returncode": returncode}
            return _reply("detached" if returncode == 0 else "attach_failed", ok=returncode == 0,
                          effect="known", handle=handle, attached=returncode == 0,
                          returncode=returncode,
                          machine={"host": session.get("host"), "generation": session.get("machine_generation")},
                          join_command=self._shell_command("attach", "--session", str(session["native_name"])))
        finally:
            try:
                with session_lock(directory, blocking=True):
                    if pin_path.exists() and read_json(pin_path, limit=4096).get("pin_id") == pin_id:
                        current_session = read_json(directory / "session.json")
                        current_session.update(last_activity_at=self.clock(),
                                               last_activity_kind="observer_detach")
                        atomic_json(directory / "session.json", current_session)
                        pin_path.unlink()
                        self._finish_retirement_locked(directory, current_session)
            except (OSError, ShellCommandError):
                pass

    def _mark_closed_operations_removed(
        self, directory: Path, operations: Sequence[Mapping[str, Any]],
    ) -> None:
        """Close retained pane receipts only after the native endpoint is gone."""
        for operation in operations:
            if operation.get("cleanup_state") == "removed":
                continue
            updated = dict(operation)
            updated["cleanup_state"] = "removed"
            updated.setdefault("collected_at", self.clock())
            atomic_json(self._paths(directory, str(operation["operation_id"]))["record"], updated)

    def _finish_retirement_locked(self, directory: Path, session: dict[str, Any]) -> bool:
        if session.get("state") != "retiring":
            return False
        operations = self._strict_ops(directory, session)
        pins, unknown = self._pins(directory, remove_dead=False)
        if (pins or unknown or not self._terminal_ops_collected(operations)
                or self._verify(session).get("state") != "missing"
                or not self._remove_stale_native(session) or not self._endpoint_absent(session)):
            return False
        self._mark_closed_operations_removed(directory, operations)
        session.update(state="removed", status="removed", cleanup_state="removed",
                       last_activity_at=self.clock(), last_activity_kind="last_viewer_detached")
        atomic_json(directory / "session.json", session)
        return True

    def _native_retirement_tick_locked(self, directory: Path,
                                       session: dict[str, Any]) -> None:
        """Advance only a drained, owner-closed canvas from exact native evidence."""
        if (_layout_mode(session) != _NATIVE_CANVAS_LAYOUT
                or not (session.get("native_layout") or {}).get("grouped_sections")):
            return
        operations = self._strict_ops(directory, session)
        drained = (all(item.get("state") in {"completed", "failed", "collected"}
                       and item.get("output_drain") in {"available", "unavailable"}
                       for item in operations)
                   and self._terminal_ops_collected(operations))
        if session.get("state") == "ready":
            runtime = _runtime_settings_for_service(session, self.source_root)
            if (owner_liveness(session.get("owner", {})) != "dead"
                    or runtime.get("auto_close_completed") is not True or not drained):
                return
            now = self.clock()
            eligible = session.get("retirement_eligible_at")
            if type(eligible) not in {int, float}:
                session["retirement_eligible_at"] = now
                atomic_json(directory / "session.json", session)
                return
            delay = float(runtime.get("auto_close_delay_seconds", 30))
            if now - float(eligible) < delay:
                return
            observed = self._verify(session)
            attached = observed.get("session", {}).get("attached")
            if observed.get("state") != "ready" or type(attached) is not int:
                session["cleanup_state"] = "unknown"
                atomic_json(directory / "session.json", session)
                return
            if attached == 0:
                # The renderer itself runs in the session being retired. The
                # independent collector can remove a detached dead-owner
                # server without killing its own control process mid-write.
                session["cleanup_state"] = "waiting_collector"
                atomic_json(directory / "session.json", session)
                return
            pins, unknown = self._pins(directory, remove_dead=False)
            if unknown or not pins:
                session["cleanup_state"] = "unknown"
                atomic_json(directory / "session.json", session)
                return
            inventory = self._native_inventory(session, observed, operations, allow_active=True)
            session.update(state="retiring", status="retiring", cleanup_state="arming",
                           closed_at=now)
            atomic_json(directory / "session.json", session)
            try:
                self.backend.retire_session(session, expected_inventory=inventory)
            except ShellCommandError as exc:
                session["cleanup_state"] = "unknown" if exc.effect == "unknown" else "pending"
                session["close_error"] = exc.code
                atomic_json(directory / "session.json", session)
                return
            session["retirement_armed_at"] = now
            session["cleanup_state"] = "waiting_viewers"
            atomic_json(directory / "session.json", session)
        if session.get("state") != "retiring" or not drained:
            return
        if type(session.get("retirement_armed_at")) not in {int, float}:
            # A lost acknowledgement is reconciled by doctor/collector; never
            # arm destroy-unattached twice from the renderer.
            return
        observed = self._verify(session)
        if observed.get("state") == "missing":
            self._finish_retirement_locked(directory, session)
            return
        if observed.get("state") != "ready":
            session["cleanup_state"] = "unknown"
            atomic_json(directory / "session.json", session)
            return
        if (observed.get("controls", {}).get("copy_mode") is True
                or (session.get("native_layout") or {}).get("overlay_lock") is not None):
            return  # Reading and dialogs protect their exact viewers.
        pins, unknown = self._pins(directory, remove_dead=False)
        if unknown:
            session["cleanup_state"] = "unknown"
            atomic_json(directory / "session.json", session)
            return
        clients = self.backend._clients(session)
        if len(clients) != observed.get("session", {}).get("attached"):
            session["cleanup_state"] = "unknown"
            atomic_json(directory / "session.json", session)
            return
        pinned = {pin.get("viewer_pid") for pin in pins}
        if any(client.get("pid") not in pinned or type(client.get("activity_at")) is not int
               for client in clients):
            session["cleanup_state"] = "unknown"
            atomic_json(directory / "session.json", session)
            return
        delay = float(_runtime_settings_for_service(session, self.source_root).get(
            "auto_close_delay_seconds", 30))
        detach = getattr(self.backend, "detach_native_client", None)
        if not callable(detach):
            session["cleanup_state"] = "pending"
            atomic_json(directory / "session.json", session)
            return
        receipts = session.get("retirement_detaches", {})
        if not isinstance(receipts, dict):
            session["cleanup_state"] = "unknown"
            atomic_json(directory / "session.json", session)
            return
        receipts = dict(receipts)
        active_pids = {str(client["pid"]) for client in clients}
        for pid_text, receipt in list(receipts.items()):
            if not isinstance(receipt, Mapping):
                session["cleanup_state"] = "unknown"
                atomic_json(directory / "session.json", session)
                return
            if pid_text not in active_pids and receipt.get("state") in {"dispatching", "unknown"}:
                receipts[pid_text] = dict(receipt) | {"state": "verified"}
        session["retirement_detaches"] = receipts
        if (session.get("cleanup_state") == "unknown"
                and all(receipt.get("state") == "verified" for receipt in receipts.values())):
            session["cleanup_state"] = "waiting_viewers"
        atomic_json(directory / "session.json", session)
        for client in clients:
            pid_text = str(client["pid"])
            if pid_text in receipts:
                if receipts[pid_text].get("state") != "verified":
                    session["cleanup_state"] = "unknown"
                    atomic_json(directory / "session.json", session)
                continue
            if self.clock() - client["activity_at"] >= delay:
                receipts[pid_text] = {"state": "dispatching", "activity_at": client["activity_at"],
                                      "started_at": self.clock()}
                session["retirement_detaches"] = receipts
                atomic_json(directory / "session.json", session)
                try:
                    detach(session, client["pid"])
                except ShellCommandError as exc:
                    receipts[pid_text]["state"] = "unknown" if exc.effect == "unknown" else "failed"
                    receipts[pid_text]["error"] = exc.code
                    session["cleanup_state"] = "unknown" if exc.effect == "unknown" else "pending"
                    session["retirement_detaches"] = receipts
                    atomic_json(directory / "session.json", session)
                    return
                receipts[pid_text]["state"] = "verified"
                session["retirement_detaches"] = receipts
                atomic_json(directory / "session.json", session)

    def close(self, handle: str, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Close requires a session handle.")
        self._authorize(session, owner)
        with session_lock(directory):
            session = read_json(directory / "session.json")
            operations = self._strict_ops(directory, session)
            active = [op for op in operations
                      if op.get("state") not in {"completed", "failed", "collected"}
                      or op.get("output_drain") == "pending"]
            pins, pin_unknown = self._pins(directory)
            observed = self._verify(session)
            attached = observed.get("session", {}).get("attached")
            if active or pin_unknown or (pins and not attached):
                return _reply("busy", ok=False, handle=handle, cleanup_state="pending",
                              active_operations=len(active), observer_pins=len(pins),
                              observer_state="unknown" if pin_unknown or type(attached) is not int else attached)
            if observed.get("state") == "missing":
                if (not self._terminal_ops_collected(operations)
                        or not self._remove_stale_native(session)
                        or not self._endpoint_absent(session)):
                    session.update(state="cleanup_pending", cleanup_state="pending")
                    atomic_json(directory / "session.json", session)
                    return _reply("cleanup_pending", ok=False, effect="unknown", handle=handle,
                                  cleanup_state="pending", removed=False)
                if session.get("state") in {"removed", "closed"}:
                    self._mark_closed_operations_removed(directory, operations)
                    return _reply("removed", effect="known", handle=handle,
                                  cleanup_state="removed", removed=True)
                self._mark_closed_operations_removed(directory, operations)
                session.update(state="removed", status="removed", cleanup_state="removed",
                               closed_at=self.clock(), last_activity_at=self.clock())
                atomic_json(directory / "session.json", session)
                return _reply("removed", effect="known", handle=handle,
                              cleanup_state="removed", removed=True)
            if observed.get("state") != "ready":
                session.update(state="cleanup_pending", cleanup_state="unknown")
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect="unknown", handle=handle, cleanup_state="unknown")
            if (_layout_mode(session) == _NATIVE_CANVAS_LAYOUT
                    and any(pane.get("role") == "operation" and pane.get("dead") is not True
                            for window in observed.get("windows", []) for pane in window.get("panes", []))):
                # A runner publishes its collected receipt just before its PTY
                # exits. Do not race that final transition during exact close.
                return _reply("busy", ok=False, effect="none", handle=handle,
                              cleanup_state="waiting_native_exit")
            if type(attached) is not int or attached > 0:
                if type(attached) is not int:
                    return _reply("cleanup_pending", ok=False, effect="unknown", handle=handle,
                                  cleanup_state="unknown", observer_state="unknown")
                expected_inventory = self._native_inventory(session, observed, operations)
                if attached > 0:
                    session.update(state="retiring", status="retiring", cleanup_state="waiting_viewers",
                                   closed_at=session.get("closed_at", self.clock()), last_activity_at=self.clock())
                    atomic_json(directory / "session.json", session)
                    try:
                        self.backend.retire_session(session, expected_inventory=expected_inventory)
                    except ShellCommandError as exc:
                        return _reply("cleanup_pending", ok=False, effect=exc.effect, handle=handle,
                                      cleanup_state="waiting_viewers", observer_state=attached,
                                      close_error=exc.code)
                    session["retirement_armed_at"] = self.clock()
                    atomic_json(directory / "session.json", session)
                    return _reply("retiring", effect="known", handle=handle,
                                  cleanup_state="waiting_viewers", observer_state=attached,
                                  closed_for_dispatch=True, removed=False)
            expected_inventory = self._native_inventory(session, observed, operations)
            try:
                self.backend.close_session(session, expected_inventory=expected_inventory)
            except ShellCommandError as exc:
                exists = Path(session["endpoint"]).exists()
                session.update(state="cleanup_pending", status="cleanup_pending",
                               cleanup_state="pending" if exists else "unknown",
                               close_error=exc.code, closed_at=self.clock())
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect=exc.effect,
                              handle=handle, cleanup_state=session["cleanup_state"], removed=False)
            exists = not self._endpoint_absent(session)
            if not exists:
                self._mark_closed_operations_removed(directory, operations)
            session.update(state="cleanup_pending" if exists else "removed",
                           status="cleanup_pending" if exists else "removed",
                           cleanup_state="pending" if exists else "removed",
                           closed_at=self.clock(), last_activity_at=self.clock())
            atomic_json(directory / "session.json", session)
            return _reply(session["state"], effect="known", handle=handle,
                          cleanup_state=session["cleanup_state"], removed=not exists)

    def recover(
        self, handle: str, *, owner: Mapping[str, Any], adopt: bool = False,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Recovery requires a session handle.")
        replacement = _owner(owner)
        scope = str(session.get("scope_key", ""))
        if _scope(replacement) != scope:
            raise ShellCommandError("recovery_scope_mismatch", "Recovery requires the same admitted agent scope.")
        owner_dir = self._owner_dir(scope)
        # Keep the same owner-index -> session lock order used by ensure. The
        # intent survives a lost acknowledgement between native marker and
        # the two private receipts, so retry never dispatches a command.
        with session_lock(owner_dir):
            owner_path = owner_dir / "owner.json"
            prior = read_json(owner_path)
            if prior.get("active_handle") != handle or prior.get("scope_key") != scope:
                raise ShellCommandError("owner_identity_unknown", "The owner index no longer names this session.", effect="unknown")
            with session_lock(directory):
                current = read_json(directory / "session.json")
                if current.get("handle") != handle or current.get("scope_key") != scope:
                    raise ShellCommandError("session_identity_unknown", "The recovery session identity changed.", effect="unknown")
                if current.get("state") != "ready":
                    raise ShellCommandError("session_identity_unknown", "Only a ready native session can be adopted.", effect="unknown")
                intent = current.get("adoption_intent")
                new_fingerprint = owner_fingerprint(replacement)
                if intent is not None:
                    if (not isinstance(intent, dict) or intent.get("owner_fingerprint") != new_fingerprint
                            or intent.get("owner") != replacement):
                        raise ShellCommandError("adoption_identity_unknown", "A different recovery intent is pending.", effect="unknown")
                    former_owner = intent.get("previous_owner")
                    former_fingerprint = intent.get("previous_fingerprint")
                else:
                    former_owner = current.get("owner")
                    former_fingerprint = current.get("owner_fingerprint")
                if not isinstance(former_owner, dict) or owner_fingerprint(former_owner) != former_fingerprint:
                    raise ShellCommandError("owner_identity_unknown", "The previous owner receipt is inconsistent.", effect="unknown")
                prior_fingerprint = owner_fingerprint(prior.get("owner", {}))
                if prior_fingerprint not in ({former_fingerprint, new_fingerprint} if intent else {former_fingerprint}):
                    raise ShellCommandError("owner_identity_unknown", "The owner index changed during recovery.", effect="unknown")
                if intent is None and former_fingerprint == new_fingerprint:
                    if prior_fingerprint != new_fingerprint or self._verify(current).get("state") != "ready":
                        raise ShellCommandError("session_identity_unknown", "The adopted session cannot be verified.", effect="unknown")
                    return self._session_reply(current, created=False, joined=True) | {"recovered": True}
                if owner_liveness(former_owner) != "dead":
                    raise ShellCommandError("owner_not_proven_dead", "The previous owner did not remain proven dead.", effect="unknown")
                observed = self.backend.inspect_session(current)
                options = observed.get("options", {})
                expected = {
                    "api": "1", "handle": handle, "key": current.get("session_key"),
                    "machine": current.get("machine_generation"),
                    "profile": current.get("settings_digest"),
                    "status_window": current.get("status_window_id"),
                }
                permitted_markers = {former_fingerprint, new_fingerprint} if intent else {former_fingerprint}
                if (observed.get("state") != "ready"
                        or any(options.get(key) != value for key, value in expected.items())
                        or options.get("owner") not in permitted_markers):
                    if not adopt:
                        return _reply("observed", handle=handle, previous_owner="dead",
                                      native_state=observed.get("state"), adoption_required=True)
                    raise ShellCommandError("session_identity_unknown", "Recovery will not adopt an unverified native session.", effect="unknown")
                if not adopt:
                    return _reply("observed", handle=handle, previous_owner="dead",
                                  native_state="ready", adoption_required=True)
                operations = self._strict_ops(directory, current)
                pins, unknown = self._pins(directory)
                if pins or unknown:
                    raise ShellCommandError("observer_blocks_recovery", "Live or unknown observers prevent recovery.")
                attached = observed.get("session", {}).get("attached")
                if type(attached) is not int or attached != 0:
                    raise ShellCommandError("observer_blocks_recovery", "Native observer count is nonzero or unknown.", effect="unknown")
                self._native_inventory(current, observed, operations, allow_active=True)
                if intent is None:
                    intent = {
                        "owner": replacement, "owner_fingerprint": new_fingerprint,
                        "previous_owner": former_owner, "previous_fingerprint": former_fingerprint,
                        "started_at": self.clock(),
                    }
                    current["adoption_intent"] = intent
                    atomic_json(directory / "session.json", current)
                adopted = {**current, "owner": replacement, "owner_fingerprint": new_fingerprint}
                self.backend.set_owner_identity(adopted, allowed_previous_owners=permitted_markers | {new_fingerprint})
                current.update(owner=replacement, owner_fingerprint=new_fingerprint,
                               state="ready", status="ready", recovered_at=self.clock(),
                               last_activity_at=self.clock())
                atomic_json(directory / "session.json", current)
                atomic_json(owner_path, {
                    "schema_version": 1, "scope_key": scope, "owner": replacement,
                    "owner_fingerprint": new_fingerprint, "active_handle": handle,
                    "updated_at": self.clock(),
                })
                current["adoption_intent"] = None
                atomic_json(directory / "session.json", current)
                return self._session_reply(current, created=False, joined=True) | {"recovered": True}

    def apply_profile(
        self, session_handle: str, resolved_settings: Mapping[str, Any], profile_digest: str,
        *, owner: Mapping[str, Any] | None = None, profile_name: str = "agent-shell",
        profile_base: str | None = None,
    ) -> dict[str, Any]:
        directory, session, op_id = self._load(session_handle)
        if op_id is not None:
            raise ShellCommandError("session_handle_required", "Applying a profile requires a session handle.")
        self._authorize(session, owner)
        new_settings = _settings(resolved_settings, source_root=self.source_root,
                                 profile_name=profile_name, profile_base=profile_base)
        if not isinstance(profile_digest, str) or not _HEX64.fullmatch(profile_digest):
            raise ShellCommandError("invalid_profile_digest", "A validated profile digest is required.")
        if _profile_digest(new_settings, profile_name) != profile_digest:
            raise ShellCommandError("profile_digest_mismatch", "The profile digest does not match the resolved settings.")
        requested_settings = new_settings
        if _family_like(session):
            runtime = _runtime_settings_for_service(session, self.source_root)
            new_settings = (_canvas_effective_settings(new_settings, runtime,
                                                       native=bool((session.get("native_layout") or {}).get("grouped_sections")))
                            if _layout_mode(session) in {_CANVAS_LAYOUT, _NATIVE_CANVAS_LAYOUT} else
                            _family_effective_settings(new_settings, runtime))
        with session_lock(directory):
            session = read_json(directory / "session.json")
            self._authorize(session, owner)
            if session.get("settings_digest") == profile_digest and session.get("settings") == new_settings:
                return _reply("unchanged", handle=session_handle, settings_digest=profile_digest,
                              cleanup_state=session.get("cleanup_state", "none"))
            active = [op for _, op in self._ops(directory)
                      if op.get("state") not in {"completed", "failed", "collected"}
                      or op.get("output_drain") == "pending"]
            pins, pin_unknown = self._pins(directory)
            observed = self._verify(session)
            attached = observed.get("session", {}).get("attached")
            if active or pins or pin_unknown or type(attached) is not int or attached > 0:
                raise ShellCommandError("profile_session_busy", "Profile settings cannot change during commands or observation.")
            if observed.get("state") != "ready":
                raise ShellCommandError("session_identity_unknown", "The exact session cannot be verified for profile changes.", effect="unknown")
            if new_settings["history_limit"] != session["settings"]["history_limit"]:
                raise ShellCommandError("profile_history_requires_new_session", "Existing tmux panes cannot change history capacity; create a new session with this profile.")
            session["profile_target"] = {
                "settings": new_settings, "digest": profile_digest, "started_at": self.clock(),
            }
            atomic_json(directory / "session.json", session)
            try:
                applied = self.backend.apply_session_settings(session, new_settings, profile_digest)
                if applied.get("state") != "applied" or applied.get("settings") != new_settings:
                    raise ShellCommandError("profile_readback_mismatch", "tmux did not read back the requested settings.", effect="unknown")
                session.update(settings=new_settings, requested_settings=requested_settings,
                               settings_digest=profile_digest,
                               profile_name=profile_name, profile_base=profile_base,
                               profile_state="applied", profile_target=None,
                               profile_applied_at=self.clock(), last_activity_at=self.clock(),
                               last_activity_kind="profile_apply")
                atomic_json(directory / "session.json", session)
                return _reply("applied", effect="known", handle=session_handle,
                              settings_digest=profile_digest, cleanup_state="none",
                              readback=applied.get("settings"))
            except Exception as exc:
                session.update(profile_state="partial", cleanup_state="pending",
                               profile_error=getattr(exc, "code", exc.__class__.__name__))
                atomic_json(directory / "session.json", session)
                return _reply("cleanup_pending", ok=False, effect="unknown",
                              handle=session_handle, settings_digest=profile_digest,
                              cleanup_state="pending", error=session["profile_error"])

    def gc(self, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Collect only proven dead-owner sessions after quiet grace; never starts a scheduler."""
        result: dict[str, list[str]] = {"removed": [], "pending": [], "preserved": [], "expired_logs": []}
        now = self.clock()
        caller = _owner(owner) if owner is not None else make_owner_binding(owner_pid=os.getpid())
        caller_fingerprint = owner_fingerprint(caller)
        if not self.root.exists():
            return _reply("completed", effect="known", gc=result)
        try:
            roots = [p for p in self._sessions(create=False).iterdir() if p.is_dir() and not p.is_symlink()]
            directories = [d for root in roots for d in root.iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            return _reply("partial", ok=False, effect="unknown", gc=result)
        for directory in directories:
            pending_handle = directory.name
            try:
                session = read_json(directory / "session.json")
                pending_handle = str(session.get("handle", directory.name))
                if self._handle(str(session.get("scope_key")), str(session.get("session_id"))) != session.get("handle"):
                    result["preserved"].append(str(directory.name))
                    continue
                with session_lock(directory):
                    session = read_json(directory / "session.json")
                    life = owner_liveness(session.get("owner", {}))
                    if (_family_like(session)
                            and caller_fingerprint != session.get("owner_fingerprint")
                            and (life != "dead" or caller.get("uid") != session.get("owner", {}).get("uid")
                                 or caller.get("machine_generation") != session.get("machine_generation"))):
                        continue
                    if life == "unknown":
                        result["preserved"].append(session["handle"])
                        continue
                    result["expired_logs"].extend(self._expire_completed_logs(directory, session, now))
                    if life != "dead":
                        result["preserved"].append(session["handle"])
                        continue
                    # A dead owner does not make its child runner dead. Advance
                    # only the exact recorded operation through drain/finalize
                    # before deciding whether the session is idle.
                    for _, operation in self._ops(directory):
                        self._reconcile(directory, session, operation)
                    operations = self._strict_ops(directory, session)
                    quiet_at = float(session.get("last_activity_at", session.get("created_at", now)))
                    for operation in operations:
                        for key in ("created_at", "started_at", "command_finished_at", "finished_at",
                                    "cancel_requested_at"):
                            value = operation.get(key)
                            if type(value) in {int, float}:
                                quiet_at = max(quiet_at, float(value))
                        cursor_path = self._paths(directory, str(operation["operation_id"]))["cursor"]
                        if cursor_path.exists():
                            with log_lock(cursor_path):
                                cursor_meta = read_json(cursor_path, limit=4096)
                            updated = cursor_meta.get("updated_at")
                            if type(updated) in {int, float}:
                                quiet_at = max(quiet_at, float(updated))
                    age = now - quiet_at
                    grace = int(session.get("settings", {}).get("idle_grace_seconds", 900))
                    active = [op for op in operations
                              if op.get("state") not in {"completed", "failed", "collected"}
                              or op.get("output_drain") == "pending"]
                    pins, pin_unknown = self._pins(directory)
                    native = self._verify(session) if session.get("native_session_id") else {"state": "missing"}
                    attached = native.get("session", {}).get("attached")
                    if (age < grace or active or pins or pin_unknown
                            or type(attached) is not int and native.get("state") == "ready"
                            or type(attached) is int and attached > 0):
                        result["preserved"].append(session["handle"])
                        continue
                    if native.get("state") == "ready":
                        try:
                            expected_inventory = self._native_inventory(session, native, operations)
                            self.backend.close_session(session, expected_inventory=expected_inventory)
                        except ShellCommandError:
                            session.update(state="cleanup_pending", cleanup_state="pending", gc_at=now)
                            atomic_json(directory / "session.json", session)
                            result["pending"].append(session["handle"])
                            continue
                    elif native.get("state") == "missing":
                        if (not self._terminal_ops_collected(operations)
                                or not self._remove_stale_native(session)
                                or not self._endpoint_absent(session)):
                            result["pending"].append(session["handle"])
                            continue
                    elif native.get("state") == "unknown":
                        result["pending"].append(session["handle"])
                        continue
                    exists = not self._endpoint_absent(session)
                    if not exists:
                        self._mark_closed_operations_removed(directory, operations)
                    session.update(state="cleanup_pending" if exists else "removed",
                                   cleanup_state="pending" if exists else "removed", gc_at=now)
                    atomic_json(directory / "session.json", session)
                    result["pending" if exists else "removed"].append(session["handle"])
            except (OSError, ShellCommandError):
                result["pending"].append(pending_handle)
        return _reply("partial" if result["pending"] else "completed",
                      effect="unknown" if result["pending"] else "known", gc=result)
