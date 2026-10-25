"""Managed, observation-only native client activity for one shell family."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from ..branding.terminal import color_capable, render_parts
from ..cli_help import ArgumentParser
from .command_activity import Activity, observation
from .command_activity_codex import CodexObserver
from .command_state import atomic_json, owner_liveness, read_json, session_lock


def start(service, handle, thread, owner, *, restart=False):
    directory, session, operation_id = service._load(handle)
    service._authorize(session, owner)
    if operation_id is not None:
        raise ValueError("client observation requires a root session")
    workspace = Path(service.backend._checked(str(session["endpoint"]), ["display-message", "-p",
        "-t", str(session["native_session_id"]), "#{session_path}"]).strip()).resolve(strict=True)
    existing = session.get("client_activity_binding")
    if existing and existing.get("thread") != thread:
        raise ValueError("another native client is already bound")
    # The existing command owner reconciles this exact request by ID and digest;
    # a lost acknowledgement never launches a duplicate observer.
    generation = existing.get("generation", 0) if existing else 0
    if type(generation) is not int or not 0 <= generation < 1_000_000:
        raise ValueError("client observation generation is invalid")
    if restart and existing and existing.get("operation_handle"):
        previous = service.status(existing["operation_handle"], owner=owner)
        if previous.get("process_scope") != "collected":
            raise ValueError("the existing client observer is still running or unverified")
        generation += 1
    identity = "client-activity" + ("-" + str(generation) if generation else "")
    result = service.run(handle, operation_id=identity, owner=owner, cwd=workspace,
        argv=[sys.executable, "-m", __name__, "--session-record", str(directory / "session.json"),
              "--thread", thread, "--workspace", str(workspace)])
    with session_lock(directory, blocking=True):
        current = read_json(directory / "session.json")
        latest = current.get("client_activity_binding") or {}
        if latest.get("thread", thread) != thread:
            raise RuntimeError("client observation binding changed")
        if latest.get("generation", -1) > generation:
            return {"state": latest["state"], "operation_handle": latest["operation_handle"]}
        state = "unavailable" if result.get("state") in {"completed", "failed"} else "observing"
        current["client_activity_binding"] = {"thread": thread, "operation_id": identity,
            "generation": generation, "state": state, "operation_handle": result["operation_handle"]}
        atomic_json(directory / "session.json", current)
    return {"state": state, "operation_handle": result["operation_handle"]}


def read_activity(directory, session):
    path = directory / "client-activity.json"
    if not path.exists():
        return {"status": "unavailable"}
    value = read_json(path, limit=2_000_000)
    if value.get("session") != session["handle"]:
        raise ValueError("client activity snapshot differs from this session")
    maximum_age = value.get("stale_after_seconds", 10)
    if type(maximum_age) is not int or not 10 <= maximum_age <= 180:
        raise ValueError("client activity freshness bound is invalid")
    if value.get("status") in {"starting", "live"} and time.time() - value.get("updated_at", 0) > maximum_age:
        value["status"] = "stale"
    return value


def stop(service, handle, owner):
    _directory, session, operation_id = service._load(handle)
    service._authorize(session, owner)
    if operation_id is None and (binding := session.get("client_activity_binding")):
        operation = binding.get("operation_handle")
        if operation:
            service.cancel(operation, owner=owner)


def main(argv=None):
    from ..process_identity import set_process_name
    set_process_name()
    parser = ArgumentParser()
    parser.add_argument("--session-record", type=Path, required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    path = args.session_record
    session = read_json(path, limit=2_000_000)
    observer = CodexObserver(args.thread, session["owner"], args.workspace)
    activity = Activity()
    output = path.parent / "client-activity.json"
    snapshot = {"schema_version": 1, "session": session["handle"], "thread": args.thread,
                "status": "starting", "coverage": "native retained item snapshots; no reasoning or payloads",
                "items": [], "labels": {}, "history_gap": False, "updated_at": time.time()}
    atomic_json(output, snapshot)
    try:
        observer.connect()
        last_write = 0
        print("[LS] client activity | read-only metadata | bounded retained history", flush=True)
        while True:
            current = read_json(path, limit=2_000_000)
            if (current.get("state") != "ready" or owner_liveness(current["owner"]) != "live"
                    or current.get("runtime_settings", {}).get("client_activity_enabled", True) is False):
                break
            labels, entries, gap = observer.snapshot()
            changed = labels != snapshot["labels"]
            for thread, entry in entries:
                value = observation(thread, entry)
                if value is None or not activity.accept(value):
                    continue
                changed = True
                role = "error" if value["state"] == "Failed" else "success" if value["state"] == "Completed" else "value"
                print(render_parts([("[LS] ", "brand"), (labels[thread] + " | ", "label"),
                    (value["name"] + " | ", "value"), (value["state"], role)],
                    color=color_capable(sys.stdout), width=80), flush=True)
            interval = current.get("runtime_settings", {}).get("client_activity_interval_seconds", 1)
            history_gap = gap or activity.dropped > 0
            changed |= history_gap != snapshot["history_gap"]
            if changed or time.monotonic() - last_write >= 5:
                snapshot.update(status="live", labels=labels, items=list(activity.items.values())[-128:],
                    retained_items=len(activity.items), stale_after_seconds=max(10, 3*interval),
                    history_gap=history_gap, updated_at=time.time())
                atomic_json(output, snapshot)
                last_write = time.monotonic()
            time.sleep(interval)
        snapshot.update(status="finished", updated_at=time.time())
        atomic_json(output, snapshot)
        return 0
    except Exception as error:
        # Client payloads and transport exception messages are private; a
        # presentation failure must never print them or interrupt other work.
        snapshot.update(status="unavailable", error_code=type(error).__name__, updated_at=time.time())
        atomic_json(output, snapshot)
        print("[LS] client activity unavailable; command panes remain observable", flush=True)
        return 3
    finally:
        observer.close()


if __name__ == "__main__":
    raise SystemExit(main())
