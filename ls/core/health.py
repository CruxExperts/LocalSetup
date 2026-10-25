from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import shlex
import uuid
import os
import re
import stat
import time
import traceback
from typing import Any

from .git_state import git_status_snapshot
from .handoff import agent_prompt_payload
from .handoff import render_feedback_draft
from .lockfile import load_json, save_json, save_text
from .redaction import redact_text
from .source import source_commit


SCHEMA_VERSION = 1
_MAX_FAILURE_TEXT = 12_000
_MAX_EVENT_PAGE_BYTES = 1_048_576


def localsetup_home(home: Path) -> Path:
    return home / ".local" / "share" / "localsetup"


def health_root(home: Path) -> Path:
    return localsetup_home(home) / "state" / "health"


def incident_root(home: Path) -> Path:
    from .github_repo.state import open_private_directory
    root = health_root(home) / "incidents"
    descriptor = open_private_directory(root, create=True)
    os.close(descriptor)
    if root.is_symlink():
        raise PermissionError("private feedback incident directory is a symlink")
    info = root.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("private feedback incident directory is not user-owned mode 0700")
    return root


def record_failure(*, repo_root: Path, home: Path, target_root: Path | None,
                   operation: str, error: Exception, argv: list[str] | None = None,
                   auto_draft: bool = True) -> dict[str, Any] | None:
    """Best-effort private failure record; never replace the caller's error."""
    try:
        exception_text = redact_text(str(error))[:_MAX_FAILURE_TEXT]
        trace = redact_text("".join(traceback.format_exception(type(error), error, error.__traceback__)))[:_MAX_FAILURE_TEXT]
        code = getattr(error, "code", type(error).__name__)
        effect = getattr(error, "effect", "none")
        event_id = f"{time.time_ns():016x}{uuid.uuid4().hex[:16]}"
        event = {
            "schema_version": "localsetup.feedback-incident.v1",
            "event_id": event_id,
            "recorded_at": _now(),
            "operation": redact_text(operation)[:160],
            "error_type": type(error).__name__,
            "code": redact_text(str(code))[:160],
            "effect": str(effect) if str(effect) in {"none", "known", "unknown"} else "unknown",
            "message": exception_text,
            "traceback": trace,
            "argv_shape": [
                (part.split("=", 1)[0][:80] if str(part).startswith("-") else
                 str(part)[:80] if index == 0 and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", str(part)) else
                 "[argument]")
                for index, part in enumerate((argv or [])[:24])
            ],
            "source_commit": _safe_source_commit(repo_root),
            "source_root": str(repo_root),
            "target_root": str(target_root) if target_root is not None else None,
            "policy": {"capture": "private_only", "publication": "explicit_exact_digest_only"},
        }
        for _ in range(32):
            if len((json.dumps(event, indent=2, sort_keys=True) + "\n").encode("utf-8")) <= 65_536:
                break
            field = max(("traceback", "message"), key=lambda key: len(event[key]))
            if not event[field]:
                raise ValueError("feedback provenance exceeds its byte bound")
            event[field] = event[field][:len(event[field]) // 2]
            event["diagnostic_truncated"] = True
        else:
            raise ValueError("feedback event exceeds its byte bound")
        directory = incident_root(home) / event_id
        directory.mkdir(mode=0o700)
        save_json(directory / "event.json", event)
        if auto_draft:
            draft = render_feedback_draft(event)
            save_text(directory / "draft.md", draft)
        return {"event_id": event_id, "path": str(directory / "event.json")}
    except Exception:
        return None


def page_events(*, home: Path, after: str | None = None, limit: int = 20,
                max_bytes: int = 65_536) -> dict[str, Any]:
    """Page bounded private incident summaries using a stable event ID cursor."""
    if not 1 <= limit <= 100 or not 1024 <= max_bytes <= _MAX_EVENT_PAGE_BYTES:
        raise ValueError("event page limit or max_bytes is outside its bound")
    if after is not None and (len(after) != 32 or any(ch not in "0123456789abcdef" for ch in after)):
        raise ValueError("event cursor is invalid")
    root = incident_root(home)
    paths = sorted(root.glob("*/event.json"), key=lambda path: path.parent.name)
    events: list[dict[str, Any]] = []
    used = 0
    more = False
    for path in paths:
        event_id = path.parent.name
        if after is not None and event_id <= after:
            continue
        from .health_feedback import event_details
        event = event_details(home=home, event_id=event_id)["event"]
        summary = {key: event.get(key) for key in
                   ("event_id", "recorded_at", "operation", "error_type", "code", "effect", "message", "source_commit")}
        if isinstance(summary.get("message"), str):
            summary["message"] = summary["message"][:512]
        size = len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        if not events and size > max_bytes:
            raise ValueError("the next event exceeds max_bytes")
        if len(events) >= limit or used + size > max_bytes:
            more = True
            break
        events.append(summary)
        used += size
    return {"ok": True, "events": events, "next_cursor": events[-1]["event_id"] if events else after,
            "has_more": more, "bytes": used}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _target_hash(target_root: Path) -> str:
    return hashlib.sha256(str(target_root).encode("utf-8")).hexdigest()[:16]


def _repo_summary_paths(target_root: Path) -> tuple[Path, Path]:
    state = target_root / ".localsetup"
    return state / "health.json", state / "AGENT_STATUS.md"


def _safe_source_commit(repo_root: Path) -> str:
    try:
        return source_commit(repo_root)
    except Exception:
        return "unknown"


def _ensure_repo_excludes(target_root: Path) -> None:
    probe = subprocess.run(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=target_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        return
    exclude_text = getattr(probe, "stdout", "").strip()
    if not exclude_text:
        return
    exclude = Path(exclude_text)
    if not exclude.is_absolute():
        exclude = target_root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    additions = [
        ".localsetup/health.json",
        ".localsetup/AGENT_STATUS.md",
        ".localsetup/install-journal/",
        ".localsetup/backups/",
        ".localsetup/state/",
        ".localsetup/context-index/",
    ]
    missing = [item for item in additions if item not in existing.splitlines()]
    if missing:
        suffix = "" if existing.endswith("\n") or not existing else "\n"
        exclude.write_text(existing + suffix + "\n".join(missing) + "\n", encoding="utf-8")


def next_actions_for(status: str, blockers: list[str] | None, target_root: Path) -> list[str]:
    if blockers:
        return ["localsetup doctor repair --target-directory " + str(target_root)]
    if status == "ok":
        return ["localsetup verify --target-directory " + str(target_root)]
    return ["localsetup doctor --target-directory " + str(target_root)]


def render_agent_status(summary: dict) -> str:
    blockers = summary.get("blockers") or []
    warnings = summary.get("warnings") or []
    actions = summary.get("next_actions") or []
    lines = [
        "# LocalSetup Agent Status",
        "",
        f"- Status: `{summary.get('status', 'unknown')}`",
        f"- Operation: `{summary.get('operation', 'unknown')}`",
        f"- Updated: `{summary.get('updated_at', '')}`",
        f"- Target: `{summary.get('target_root', '')}`",
        "",
        "## Blockers",
        "",
    ]
    lines.extend([f"- {item}" for item in blockers] or ["- None"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["- None"])
    lines.extend(["", "## Next Actions", ""])
    lines.extend([f"- `{item}`" for item in actions] or ["- None"])
    return "\n".join(lines) + "\n"


def write_health_event(
    *,
    repo_root: Path,
    home: Path,
    target_root: Path,
    operation: str,
    mode: str,
    status: str,
    payload: dict | None = None,
    blockers: list[str] | None = None,
    warnings: list[str] | None = None,
    decisions: list[dict] | None = None,
    backups: list[str] | None = None,
    journal_path: str | None = None,
    report_path: str | None = None,
    git_pre: dict | None = None,
    git_post: dict | None = None,
    localsetup_created_delta: dict | None = None,
) -> dict:
    target = target_root.expanduser().resolve(strict=False)
    root = health_root(home)
    event_id = uuid.uuid4().hex
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "operation": operation,
        "mode": mode,
        "status": status,
        "started_at": (payload or {}).get("started_at"),
        "finished_at": _now(),
        "source": {
            "root": str(repo_root),
            "commit": _safe_source_commit(repo_root),
        },
        "home": str(home),
        "package_root": str(localsetup_home(home) / "packages"),
        "registry_path": str(localsetup_home(home) / "registry.json"),
        "target_root": str(target),
        "selectors": (payload or {}).get("selectors", {}),
        "platforms": (payload or {}).get("platforms", []),
        "blockers": blockers or [],
        "warnings": warnings or [],
        "decisions": decisions or [],
        "detected_shape": (payload or {}).get("detected_shape", {}),
        "inferred": (payload or {}).get("inferred", {}),
        "actions": (payload or {}).get("actions", []),
        "repaired": (payload or {}).get("repaired", []),
        "skipped": (payload or {}).get("skipped", []),
        "backups": backups or [],
        "journal": journal_path,
        "report": report_path,
        "git": {
            "pre": git_pre,
            "post": git_post,
            "localsetup_created_delta": localsetup_created_delta,
        },
        "metrics": (payload or {}).get("metrics", {}),
        "next_actions": (payload or {}).get("next_actions") or next_actions_for(status, blockers or [], target),
    }
    events = root / "events"
    targets = root / "targets"
    events.mkdir(parents=True, exist_ok=True)
    targets.mkdir(parents=True, exist_ok=True)
    save_json(events / f"{event_id}.json", event)
    save_json(root / "latest.json", event)
    save_json(targets / f"{_target_hash(target)}.json", event)
    write_repo_summary(target, event)
    return event


def write_repo_summary(target_root: Path, event: dict) -> None:
    health_json, status_md = _repo_summary_paths(target_root)
    health_json.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event.get("event_id"),
        "operation": event.get("operation"),
        "mode": event.get("mode"),
        "status": event.get("status"),
        "updated_at": event.get("finished_at"),
        "target_root": event.get("target_root"),
        "blockers": event.get("blockers", []),
        "warnings": event.get("warnings", []),
        "decisions": event.get("decisions", []),
        "backups": event.get("backups", []),
        "journal": event.get("journal"),
        "report": event.get("report"),
        "next_actions": event.get("next_actions", []),
    }
    save_json(health_json, summary)
    save_text(status_md, render_agent_status(summary))
    _ensure_repo_excludes(target_root)


def read_health_status(*, home: Path, target_root: Path | None = None) -> dict:
    path = health_root(home) / "latest.json"
    if target_root is not None:
        path = health_root(home) / "targets" / f"{_target_hash(target_root.expanduser().resolve(strict=False))}.json"
    event = load_json(path)
    if event:
        return {"ok": True, "event": event, "git": git_status_snapshot(target_root or Path.cwd())}
    return {"ok": False, "event": None, "message": "no LocalSetup health events recorded"}


def repair_queue(*, home: Path) -> dict:
    targets = health_root(home) / "targets"
    items: list[dict] = []
    if targets.is_dir():
        for path in sorted(targets.glob("*.json")):
            event = load_json(path)
            if not event:
                continue
            if event.get("blockers") or event.get("decisions") or event.get("status") not in {"ok", "success"}:
                target_root = str(event.get("target_root"))
                prompt_argv = [
                    "localsetup",
                    "doctor",
                    "repair",
                    "--target-directory",
                    target_root,
                    "--repair-mode",
                    "migration-plan",
                    "--agent-prompt",
                ]
                items.append(
                    {
                        "event_id": event.get("event_id"),
                        "target_root": event.get("target_root"),
                        "status": event.get("status"),
                        "operation": event.get("operation"),
                        "blockers": event.get("blockers", []),
                        "decisions": event.get("decisions", []),
                        "decision_count": len(event.get("decisions", [])),
                        "blocker_count": len(event.get("blockers", [])),
                        "decision_kinds": sorted({str(item.get("kind")) for item in event.get("decisions", []) if isinstance(item, dict)}),
                        "blocker_kinds": sorted(
                            {
                                str(item.get("kind")) if isinstance(item, dict) else "message"
                                for item in event.get("blockers", [])
                            }
                        ),
                        "last_report_path": event.get("report"),
                        "prompt_argv": prompt_argv,
                        "prompt_command": shlex.join(prompt_argv),
                        "next_actions": event.get("next_actions", []),
                        "metrics": event.get("metrics", {}),
                    }
                )
    return {"ok": True, "items": items}


def write_repair_queue_prompts(*, home: Path, output_dir: Path) -> dict:
    queue = repair_queue(home=home)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    targets = health_root(home) / "targets"
    if targets.is_dir():
        for path in sorted(targets.glob("*.json")):
            event = load_json(path)
            if not event:
                continue
            if not (event.get("blockers") or event.get("decisions") or event.get("status") not in {"ok", "success"}):
                continue
            target_root = Path(str(event.get("target_root") or "target"))
            prompt_path = output_dir / f"{_target_hash(target_root)}-repair-handoff.md"
            prompt = agent_prompt_payload(event, path=prompt_path, source_event_id=str(event.get("event_id") or ""))
            written.append({"target_root": str(target_root), "path": prompt["path"], "context_hash": prompt["context_hash"]})
    return {"ok": True, "items": queue["items"], "written": written}
