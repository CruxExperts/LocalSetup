#!/usr/bin/env python3
"""Filesystem CLI for Arbiter Zebu decision queues.

The helper creates and reads markdown plan files under an Arbiter queue
directory. It does not contact Telegram or run the Arbiter Zebu bot; the bot or
a human reviewer is still responsible for moving completed plans into the
completed queue and recording answers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DESCRIPTION_MAX = 4096
FIELD_MAX = 512
ID_MAX = 128
POLL_INTERVAL_DEFAULT = 30
TIMEOUT_DEFAULT = 3600
PRIORITY_VALUES = {"low", "normal", "high", "urgent"}
PLAN_COMPLETION_FIELDS = {
    "answered",
    "answers",
    "completed_at",
    "created_at",
    "remaining",
    "status",
    "total",
    "updated_at",
    "version",
}
DECISION_COMPLETION_FIELDS = {"answer", "answered_at", "status"}


class InputError(ValueError):
    """Raised when user input is malformed."""


def _load_frontmatter_module():
    for parent in Path(__file__).resolve().parents:
        if (parent / "lib" / "deps.py").is_file():
            sys.path.insert(0, str(parent / "lib"))
            from deps import require_deps

            require_deps(["frontmatter"])
            break

    try:
        import frontmatter  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment guidance
        raise SystemExit(
            "Missing dependency: python-frontmatter. Run `uv sync --locked --no-dev` from the LocalSetup source checkout."
        ) from exc

    return frontmatter


def _clean_text(value: Any, *, label: str, max_len: int = FIELD_MAX, required: bool = True) -> str:
    if value is None:
        if required:
            raise InputError(f"{label}: required")
        return ""
    if not isinstance(value, str):
        raise InputError(f"{label}: expected string")
    cleaned = " ".join(value.split()).strip()
    if required and not cleaned:
        raise InputError(f"{label}: empty value")
    if len(cleaned) > max_len:
        raise InputError(f"{label}: exceeds {max_len} characters")
    return cleaned


def _slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug[:80] or fallback


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _queue_root(path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return Path.home() / ".arbiter" / "queue"


def _ensure_queue(root: Path) -> None:
    for name in ("pending", "completed", "notify"):
        (root / name).mkdir(parents=True, exist_ok=True)


def _first_env(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return _clean_text(value, label=name, required=False)
    return default


def _load_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"payload: invalid JSON at character {exc.pos}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise InputError("payload: JSON root must be an object")
    return data


def _normalize_options(raw_options: Any, decision_id: str) -> list[dict[str, str]]:
    if not isinstance(raw_options, list) or not raw_options:
        raise InputError(f"decision {decision_id}: options must be a non-empty array")
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_options, start=1):
        if not isinstance(raw, dict):
            raise InputError(f"decision {decision_id} option {index}: expected object")
        key = _clean_text(raw.get("key"), label=f"decision {decision_id} option {index} key", max_len=ID_MAX)
        if key in seen:
            raise InputError(f"decision {decision_id}: duplicate option key {key}")
        seen.add(key)
        option = {
            "key": key,
            "label": _clean_text(raw.get("label"), label=f"decision {decision_id} option {index} label"),
        }
        note = _clean_text(raw.get("note"), label=f"decision {decision_id} option {index} note", required=False)
        if note:
            option["note"] = note
        options.append(option)
    return options


def _normalize_decisions(raw_decisions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_decisions, list) or not raw_decisions:
        raise InputError("decisions: must be a non-empty array")
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_decisions, start=1):
        if not isinstance(raw, dict):
            raise InputError(f"decision {index}: expected object")
        decision_id = _clean_text(raw.get("id"), label=f"decision {index} id", max_len=ID_MAX)
        injected_fields = DECISION_COMPLETION_FIELDS.intersection(raw)
        if injected_fields:
            fields = ", ".join(sorted(injected_fields))
            raise InputError(f"decision {decision_id}: completion fields are not allowed in push: {fields}")
        if decision_id in seen:
            raise InputError(f"decision id duplicated: {decision_id}")
        seen.add(decision_id)
        allow_custom = raw.get("allowCustom", False)
        if not isinstance(allow_custom, bool):
            raise InputError(f"decision {decision_id}: allowCustom must be true or false")
        decision = {
            "id": decision_id,
            "title": _clean_text(raw.get("title"), label=f"decision {decision_id} title"),
            "context": _clean_text(
                raw.get("context"),
                label=f"decision {decision_id} context",
                max_len=DESCRIPTION_MAX,
                required=False,
            ),
            "options": _normalize_options(raw.get("options"), decision_id),
            "allowCustom": allow_custom,
            "status": "pending",
            "answer": None,
            "answered_at": None,
        }
        default = _clean_text(raw.get("default"), label=f"decision {decision_id} default", required=False)
        if default:
            option_keys = {option["key"] for option in decision["options"]}
            if default not in option_keys:
                raise InputError(f"decision {decision_id}: default must match an option key")
            decision["default"] = default
        decisions.append(decision)
    return decisions


def _normalize_plan(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    injected_fields = PLAN_COMPLETION_FIELDS.intersection(payload)
    if injected_fields:
        fields = ", ".join(sorted(injected_fields))
        raise InputError(f"plan: completion fields are not allowed in push: {fields}")
    now = _utc_now()
    priority = _clean_text(payload.get("priority", "normal"), label="priority", max_len=16)
    if priority not in PRIORITY_VALUES:
        raise InputError("priority: use low, normal, high, or urgent")
    plan_id = _clean_text(payload.get("planId") or payload.get("id") or secrets.token_hex(6), label="planId", max_len=ID_MAX)
    decisions = _normalize_decisions(payload.get("decisions"))
    agent = _clean_text(
        payload.get("agent") or args.agent or _first_env(("ARBITER_AGENT", "AGENT_ID", "USER"), "agent"),
        label="agent",
        max_len=ID_MAX,
    )
    session = _clean_text(
        payload.get("session") or args.session or _first_env(("ARBITER_SESSION", "AGENT_SESSION"), "default"),
        label="session",
        max_len=ID_MAX,
    )
    tag = _clean_text(payload.get("tag", "general"), label="tag", max_len=ID_MAX)
    total = len(decisions)
    return {
        "id": plan_id,
        "planId": plan_id,
        "version": 1,
        "agent": agent,
        "session": session,
        "tag": tag,
        "title": _clean_text(payload.get("title"), label="title"),
        "priority": priority,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "total": total,
        "answered": 0,
        "remaining": total,
        "notify_session": _clean_text(payload.get("notify"), label="notify", max_len=ID_MAX, required=False) or None,
        "context": _clean_text(payload.get("context"), label="context", max_len=DESCRIPTION_MAX, required=False),
        "decisions": decisions,
    }


def _render_plan_body(plan: dict[str, Any]) -> str:
    lines = [f"# {plan['title']}", ""]
    if plan.get("context"):
        lines.extend([str(plan["context"]), ""])
    lines.append("---")
    lines.append("")
    for index, decision in enumerate(plan["decisions"], start=1):
        lines.append(f"## Decision {index}: {decision['title']}")
        lines.append("")
        lines.append(f"id: {decision['id']}")
        lines.append(f"status: {decision.get('status', 'pending')}")
        answer = decision.get("answer")
        lines.append(f"answer: {answer if answer not in (None, '') else 'null'}")
        lines.append(f"answered_at: {decision.get('answered_at') or 'null'}")
        if decision.get("allowCustom"):
            lines.append("allow_custom: true")
        lines.append("")
        if decision.get("context"):
            lines.append(f"Context: {decision['context']}")
            lines.append("")
        lines.append("Options:")
        for option in decision["options"]:
            note = f" - {option['note']}" if option.get("note") else ""
            marker = " (default)" if decision.get("default") == option["key"] else ""
            lines.append(f"- `{option['key']}`: {option['label']}{note}{marker}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_plan(root: Path, plan: dict[str, Any]) -> Path:
    frontmatter = _load_frontmatter_module()
    filename = f"{_slug(str(plan['agent']), fallback='agent')}-{_slug(str(plan['tag']), fallback='tag')}-{_slug(str(plan['planId']), fallback='plan')}.md"
    path = root / "pending" / filename
    if path.exists():
        raise InputError(f"plan already exists: {path}")
    post = frontmatter.Post(_render_plan_body(plan), **plan)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _iter_plan_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for state in ("completed", "pending"):
        paths.extend((root / state).glob("*.md"))
    return sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)


def _load_plan(path: Path) -> tuple[dict[str, Any], str]:
    frontmatter = _load_frontmatter_module()
    post = frontmatter.load(path)
    return dict(post.metadata), str(post.content)


def _find_plan(root: Path, *, plan_id: str | None = None, tag: str | None = None) -> tuple[Path, dict[str, Any], str]:
    if not plan_id and not tag:
        raise InputError("provide a plan_id or --tag")
    clean_plan_id = _clean_text(plan_id, label="plan_id", max_len=ID_MAX, required=False) if plan_id else None
    clean_tag = _clean_text(tag, label="tag", max_len=ID_MAX, required=False) if tag else None
    for path in _iter_plan_files(root):
        metadata, content = _load_plan(path)
        if clean_plan_id and clean_plan_id not in {str(metadata.get("id", "")), str(metadata.get("planId", ""))}:
            continue
        if clean_tag and clean_tag != str(metadata.get("tag", "")):
            continue
        return path, metadata, content
    target = clean_plan_id or f"tag={clean_tag}"
    raise InputError(f"plan not found: {target}")


def _validated_answer(decision: dict[str, Any]) -> str | None:
    answer = decision.get("answer")
    if not isinstance(answer, str):
        return None
    try:
        cleaned_answer = _clean_text(answer, label="decision answer")
    except InputError:
        return None
    if decision.get("allowCustom") is True:
        return cleaned_answer

    options = decision.get("options")
    if not isinstance(options, list):
        return None
    option_keys: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            continue
        try:
            option_keys.add(_clean_text(option.get("key"), label="option key", max_len=ID_MAX))
        except InputError:
            continue
    return cleaned_answer if cleaned_answer in option_keys else None


def _validated_decision_id(value: Any) -> str | None:
    try:
        return _clean_text(value, label="decision id", max_len=ID_MAX)
    except InputError:
        return None


def _status_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    decisions = metadata.get("decisions") if isinstance(metadata.get("decisions"), list) else []
    raw_total = metadata.get("total")
    metadata_total = raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0 else 0
    total = len(decisions) if decisions else metadata_total
    answered = 0
    seen_decision_ids: set[str] = set()
    decision_status: dict[str, dict[str, Any]] = {}
    for index, decision in enumerate(decisions, start=1):
        if not isinstance(decision, dict):
            continue
        decision_id = _validated_decision_id(decision.get("id"))
        duplicate = decision_id in seen_decision_ids if decision_id is not None else False
        if decision_id is not None:
            seen_decision_ids.add(decision_id)
        answer = None if decision_id is None or duplicate else _validated_answer(decision)
        status = "answered" if answer is not None else "pending"
        if answer is not None:
            answered += 1
        display_id = decision_id or f"unknown-{index}"
        decision_status[display_id] = {"status": status, "answer": answer}
    remaining = max(0, total - answered)
    status = "completed" if decisions and remaining == 0 else "pending"
    return {
        "planId": metadata.get("planId") or metadata.get("id"),
        "title": metadata.get("title"),
        "tag": metadata.get("tag"),
        "status": status,
        "total": total,
        "answered": answered,
        "remaining": remaining,
        "decisions": decision_status,
    }


def _answers_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    seen_decision_ids: set[str] = set()
    decisions = metadata.get("decisions")
    if isinstance(decisions, list):
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            decision_id = _validated_decision_id(decision.get("id"))
            if decision_id is None or decision_id in seen_decision_ids:
                continue
            seen_decision_ids.add(decision_id)
            answer = _validated_answer(decision)
            if answer is not None:
                answers[decision_id] = answer
    return answers


def cmd_push(args: argparse.Namespace) -> int:
    root = _queue_root(args.queue_dir)
    _ensure_queue(root)
    plan = _normalize_plan(_load_payload(args.payload), args)
    path = _write_plan(root, plan)
    print(
        json.dumps(
            {
                "planId": plan["planId"],
                "file": str(path),
                "total": plan["total"],
                "status": "pending",
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _queue_root(args.queue_dir)
    _ensure_queue(root)
    path, metadata, _content = _find_plan(root, plan_id=args.plan_id, tag=args.tag)
    status = _status_from_metadata(metadata)
    status["file"] = str(path)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    root = _queue_root(args.queue_dir)
    _ensure_queue(root)
    path, metadata, _content = _find_plan(root, plan_id=args.plan_id, tag=args.tag)
    status = _status_from_metadata(metadata)
    if status["status"] != "completed":
        print(
            json.dumps(
                {
                    "error": "Plan not complete",
                    "planId": status.get("planId"),
                    "status": status["status"],
                    "remaining": status["remaining"],
                    "file": str(path),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "planId": status.get("planId"),
                "status": "completed",
                "completedAt": metadata.get("completed_at"),
                "answers": _answers_from_metadata(metadata),
                "file": str(path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_await(args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    while True:
        root = _queue_root(args.queue_dir)
        _ensure_queue(root)
        path, metadata, _content = _find_plan(root, plan_id=args.plan_id, tag=args.tag)
        status = _status_from_metadata(metadata)
        if status["status"] == "completed":
            print(
                json.dumps(
                    {
                        "planId": status.get("planId"),
                        "status": "completed",
                        "completedAt": metadata.get("completed_at"),
                        "answers": _answers_from_metadata(metadata),
                        "file": str(path),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            print(
                json.dumps(
                    {
                        "error": "Timed out waiting for plan",
                        "planId": status.get("planId"),
                        "status": status["status"],
                        "remaining": status["remaining"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        time.sleep(min(args.interval, remaining_time))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbiter_cli",
        description="Create and read Arbiter Zebu filesystem queue plans.",
    )
    parser.add_argument("--queue-dir", help="Queue root; default: ~/.arbiter/queue")
    sub = parser.add_subparsers(dest="command", required=True)

    p_push = sub.add_parser("push", help="Create a pending decision plan from a JSON object")
    p_push.add_argument("--queue-dir", default=argparse.SUPPRESS, help="Queue root; default: ~/.arbiter/queue")
    p_push.add_argument("payload", help="Plan JSON with title and decisions")
    p_push.add_argument("--agent", help="Override agent id")
    p_push.add_argument("--session", help="Override agent session")
    p_push.set_defaults(func=cmd_push)

    p_status = sub.add_parser("status", help="Check decision plan status")
    p_status.add_argument("--queue-dir", default=argparse.SUPPRESS, help="Queue root; default: ~/.arbiter/queue")
    p_status.add_argument("plan_id", nargs="?", help="Plan ID")
    p_status.add_argument("--tag", help="Find the newest plan with this tag")
    p_status.set_defaults(func=cmd_status)

    p_get = sub.add_parser("get", help="Return answers for a completed plan")
    p_get.add_argument("--queue-dir", default=argparse.SUPPRESS, help="Queue root; default: ~/.arbiter/queue")
    p_get.add_argument("plan_id", nargs="?", help="Plan ID")
    p_get.add_argument("--tag", help="Find the newest plan with this tag")
    p_get.set_defaults(func=cmd_get)

    p_await = sub.add_parser("await", help="Poll until a plan is complete or timeout is reached")
    p_await.add_argument("--queue-dir", default=argparse.SUPPRESS, help="Queue root; default: ~/.arbiter/queue")
    p_await.add_argument("plan_id", nargs="?", help="Plan ID")
    p_await.add_argument("--tag", help="Find the newest plan with this tag")
    p_await.add_argument("--timeout", type=int, default=TIMEOUT_DEFAULT, help="Seconds to wait")
    p_await.add_argument("--interval", type=int, default=POLL_INTERVAL_DEFAULT, help="Poll interval in seconds")
    p_await.set_defaults(func=cmd_await)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if getattr(args, "timeout", 1) < 0:
            raise InputError("timeout must be non-negative")
        if getattr(args, "interval", 1) <= 0:
            raise InputError("interval must be positive")
        return args.func(args)
    except InputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
