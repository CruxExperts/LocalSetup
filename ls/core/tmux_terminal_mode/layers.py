"""Layer operations for tmux terminal mode."""

from __future__ import annotations

import json
import platform
import re
import shutil
from pathlib import Path

from .constants import (
    AGENT_RULE_BLOCK,
    BAK_SUFFIX,
    DEFAULT_SESSION,
    IDE_SETTINGS_CANDIDATES,
    SENTINEL_BEGIN,
    SENTINEL_END,
    SHELL_BLOCK_TEMPLATE,
)
from .io import atomic_write, backup, die, dry, has_sentinel, info, ok, restore_or_strip, safe_read, warn


def resolve_tmux() -> str:
    result = shutil.which("tmux")
    if not result:
        die(
            "tmux not found in PATH.\n"
            "  Install tmux (e.g. sudo apt install tmux) and try again."
        )
    return result


def detect_settings_file() -> Path | None:
    for candidate in IDE_SETTINGS_CANDIDATES:
        p = Path(candidate).expanduser()
        if p.exists():
            return p
        if p.parent.exists():
            try:
                p.write_text("{}\n", encoding="utf-8")
                info(f"Created empty settings file: {p}")
            except OSError as exc:
                warn(f"Could not create {p}: {exc}; trying next candidate.")
                continue
            return p
    return None


def detect_existing_settings_file() -> Path | None:
    for candidate in IDE_SETTINGS_CANDIDATES:
        p = Path(candidate).expanduser()
        if p.exists():
            return p
    return None


def load_json_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = safe_read(path)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        die(
            f"settings.json at {path} is not valid JSON: {exc}\n"
            "  Fix the file manually or use --settings-file to point to a different path."
        )


def write_json_settings(path: Path, data: dict, dry_run: bool) -> None:
    content = json.dumps(data, indent=4) + "\n"
    if dry_run:
        dry(f"Would write IDE settings to {path}")
    else:
        atomic_write(path, content)
        ok(f"Wrote IDE settings: {path}")


def apply_ide_layer(settings_path: Path, session: str, tmux_path: str, dry_run: bool) -> None:
    backup(settings_path, dry_run)
    data = load_json_settings(settings_path)

    profiles_key = "terminal.integrated.profiles.linux"
    default_key = "terminal.integrated.defaultProfile.linux"
    profile_name = "tmux-session"

    profiles = data.get(profiles_key, {})
    profiles[profile_name] = {
        "path": tmux_path,
        "args": ["new-session", "-A", "-s", session],
        "icon": "terminal-tmux",
    }
    data[profiles_key] = profiles
    data[default_key] = profile_name

    write_json_settings(settings_path, data, dry_run)
    if not dry_run:
        ok(f"Layer 1a (IDE profile): tmux-session -> {session}")


def remove_ide_layer(settings_path: Path, dry_run: bool) -> None:
    bak = settings_path.with_suffix(settings_path.suffix + BAK_SUFFIX)
    if bak.exists():
        if dry_run:
            dry(f"Would restore IDE settings from backup: {bak} -> {settings_path}")
        else:
            shutil.copy2(str(bak), str(settings_path))
            bak.unlink()
            ok(f"Restored IDE settings from backup: {settings_path}")
        return

    if not settings_path.exists():
        info("Nothing to do for Layer 1a (settings file not found).")
        return

    data = load_json_settings(settings_path)
    profiles_key = "terminal.integrated.profiles.linux"
    default_key = "terminal.integrated.defaultProfile.linux"
    profile_name = "tmux-session"

    changed = False
    profiles = data.get(profiles_key, {})
    if profile_name in profiles:
        del profiles[profile_name]
        data[profiles_key] = profiles
        changed = True
    if default_key in data:
        del data[default_key]
        changed = True

    if not changed:
        info("Nothing to do for Layer 1a (profile not found in settings).")
        return

    write_json_settings(settings_path, data, dry_run)


def ide_layer_active(settings_path: Path | None) -> str | None:
    if not settings_path or not settings_path.exists():
        return None
    data = load_json_settings(settings_path)
    profiles = data.get("terminal.integrated.profiles.linux", {})
    profile = profiles.get("tmux-session", {})
    args = profile.get("args", [])
    if len(args) >= 3 and args[0] == "new-session" and args[1] == "-A" and args[2] == "-s":
        return args[3] if len(args) > 3 else DEFAULT_SESSION
    return None


def default_shell_rc() -> Path:
    if platform.system() == "Darwin":
        p = Path("~/.bash_profile").expanduser()
        if p.exists():
            return p
    return Path("~/.bashrc").expanduser()


def apply_shell_layer(rc_path: Path, session: str, dry_run: bool) -> None:
    if not rc_path.exists():
        if dry_run:
            dry(f"Would create {rc_path} and append shell auto-attach block")
            return
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path.write_text("", encoding="utf-8")
        info(f"Created {rc_path}")
    else:
        backup(rc_path, dry_run)

    if dry_run:
        text = safe_read(rc_path)
        if has_sentinel(text):
            info(f"Layer 1b already present in {rc_path} (idempotent, no change).")
        else:
            dry(f"Would append shell auto-attach block to {rc_path}")
        return

    text = safe_read(rc_path)
    if has_sentinel(text):
        info(f"Layer 1b already present in {rc_path} (idempotent, no change).")
        return

    block = SHELL_BLOCK_TEMPLATE.format(
        sentinel_begin=SENTINEL_BEGIN,
        sentinel_end=SENTINEL_END,
        session=session,
    )
    with open(rc_path, "a", encoding="utf-8") as f:
        f.write("\n" + block)
    ok(f"Layer 1b (shell auto-attach): appended to {rc_path}")


def remove_shell_layer(rc_path: Path, dry_run: bool) -> None:
    restore_or_strip(rc_path, dry_run, "Layer 1b (shell RC)")


def shell_layer_active(rc_path: Path) -> str | None:
    if not rc_path.exists():
        return None
    text = safe_read(rc_path)
    if not has_sentinel(text):
        return None
    m = re.search(r"exec tmux new-session -A -s (\S+)", text)
    return m.group(1) if m else DEFAULT_SESSION


def apply_rule_layer(rules_path: Path, dry_run: bool) -> None:
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    if not rules_path.exists():
        if dry_run:
            dry(f"Would create {rules_path} and append agent rule block")
            return
        rules_path.write_text("", encoding="utf-8")
        info(f"Created {rules_path}")
    else:
        backup(rules_path, dry_run)

    if dry_run:
        text = safe_read(rules_path)
        if has_sentinel(text) and rule_layer_current(rules_path):
            info(f"Layer 2 is current in {rules_path} (idempotent, no change).")
        elif has_sentinel(text):
            dry(f"Would refresh the owned agent rule block in {rules_path} and preserve surrounding content")
        else:
            dry(f"Would append agent rule block to {rules_path}")
        return

    text = safe_read(rules_path)
    if has_sentinel(text):
        if rule_layer_current(rules_path):
            info(f"Layer 2 is current in {rules_path} (idempotent, no change).")
            return
        refreshed = _refresh_owned_rule_block(text)
        atomic_write(rules_path, refreshed)
        ok(f"Layer 2 (agent rule): refreshed the owned block in {rules_path}")
        return

    with open(rules_path, "a", encoding="utf-8") as f:
        f.write("\n" + AGENT_RULE_BLOCK)
    ok(f"Layer 2 (agent rule): appended to {rules_path}")


def _refresh_owned_rule_block(text: str) -> str:
    """Replace managed sentinel content once while preserving custom surroundings."""
    lines = text.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == SENTINEL_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == SENTINEL_END]
    if not begins or len(begins) != len(ends) or any(begin >= end for begin, end in zip(begins, ends)):
        die("Layer 2 has malformed managed sentinels; preserve the file and repair its block boundaries manually.")
    spans: list[tuple[int, int]] = []
    end_cursor = 0
    for begin in begins:
        while end_cursor < len(ends) and ends[end_cursor] <= begin:
            end_cursor += 1
        if end_cursor >= len(ends):
            die("Layer 2 has malformed managed sentinels; preserve the file and repair its block boundaries manually.")
        span = (begin, ends[end_cursor] + 1)
        if spans and span[0] < spans[-1][1]:
            die("Layer 2 has nested managed sentinels; preserve the file and repair its block boundaries manually.")
        spans.append(span)
        end_cursor += 1
    first_start, first_end = spans[0]
    replacement = AGENT_RULE_BLOCK
    if not replacement.endswith("\n"):
        replacement += "\n"
    chunks: list[str] = ["".join(lines[:first_start]), replacement]
    cursor = first_end
    for start, end in spans[1:]:
        chunks.append("".join(lines[cursor:start]))
        cursor = end
    chunks.append("".join(lines[cursor:]))
    return "".join(chunks)


def remove_rule_layer(rules_path: Path, dry_run: bool) -> None:
    restore_or_strip(rules_path, dry_run, "Layer 2 (agent rule)")


def rule_layer_active(rules_path: Path) -> bool:
    if not rules_path.exists():
        return False
    return has_sentinel(safe_read(rules_path))


def rule_layer_current(rules_path: Path) -> bool:
    if not rules_path.exists():
        return False
    text = safe_read(rules_path)
    lines = text.splitlines(keepends=True)
    begins = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == SENTINEL_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == SENTINEL_END]
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        return False
    block = "".join(lines[begins[0]:ends[0] + 1]).strip()
    return block == AGENT_RULE_BLOCK.strip()


def tmux_ops_path(tools_dir: Path) -> Path | None:
    p = tools_dir / "tmux_ops"
    return p if p.exists() else None


def terminal_mode_status(
    *,
    settings_path: Path | None,
    rc_path: Path,
    rules_path: Path,
    tools_dir: Path,
) -> dict:
    ide_session = ide_layer_active(settings_path)
    shell_session = shell_layer_active(rc_path)
    rule_active = rule_layer_active(rules_path)
    rule_current = rule_layer_current(rules_path)
    tmux_ops = tmux_ops_path(tools_dir)
    if ide_session:
        detected_mode = "ide"
        detected_session = ide_session
    elif shell_session:
        detected_mode = "shell"
        detected_session = shell_session
    else:
        detected_mode = "none"
        detected_session = None
    return {
        "mode": detected_mode,
        "session": detected_session,
        "layers": {
            "ide": {
                "active": bool(ide_session),
                "session": ide_session,
                "settings_path": str(settings_path) if settings_path else None,
            },
            "shell": {
                "active": bool(shell_session),
                "session": shell_session,
                "rc_path": str(rc_path),
            },
            "rules": {
                "active": rule_active,
                "current": rule_current,
                "rules_path": str(rules_path),
            },
            "tmux_ops": {
                "present": bool(tmux_ops),
                "path": str(tmux_ops) if tmux_ops else None,
            },
        },
    }
