#!/usr/bin/env python3
"""Generic agent heartbeat harness runtime with atomic run artifacts."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    if (parent / "lib" / "deps.py").is_file():
        sys.path.insert(0, str(parent / "lib"))
        from deps import require_deps

        require_deps(["yaml"])
        break

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guidance
    raise SystemExit("Missing dependency: PyYAML. Run `uv sync --locked --no-dev` from the LocalSetup source checkout.") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_heartbeat_support import ACTIVE_NAME
from codex_heartbeat_support import DEFAULT_COMMAND_TIMEOUT
from codex_heartbeat_support import LATEST_NAME
from codex_heartbeat_support import LOCK_NAME
from codex_heartbeat_support import MAX_COMMAND_TIMEOUT
from codex_heartbeat_support import MAX_TAIL_CHARS
from codex_heartbeat_support import RUNS_DIR_NAME
from codex_heartbeat_support import SCHEMA_VERSION
from codex_heartbeat_support import STATE_DIR_DEFAULT
from codex_heartbeat_support import HeartbeatError
from codex_heartbeat_support import atomic_write_json
from codex_heartbeat_support import atomic_write_text
from codex_heartbeat_support import command_policy
from codex_heartbeat_support import config_path_for
from codex_heartbeat_support import heartbeat_enabled
from codex_heartbeat_support import load_yaml
from codex_heartbeat_support import plan_summary
from codex_heartbeat_support import planned_commands
from codex_heartbeat_support import run_command
from codex_heartbeat_support import sha256_file
from codex_heartbeat_support import write_command_sidecar
from codex_heartbeat_support import sha256_json
from codex_heartbeat_support import state_root_from_config
from codex_heartbeat_support import utc_now
from codex_heartbeat_support import validate_direct_command

def _ensure_state_dirs(state_root: Path) -> Path:
    runs = state_root / RUNS_DIR_NAME
    runs.mkdir(parents=True, exist_ok=True)
    return runs


def _is_relative_child(value: str, base: Path) -> Path:
    if not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise HeartbeatError("pointer path must be a relative path inside state")
    resolved = (base / value).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise HeartbeatError("pointer path escapes heartbeat state") from exc
    return resolved


def validate_active_pointer(state_root: Path) -> dict[str, Any] | None:
    pointer = state_root / ACTIVE_NAME
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HeartbeatError("active pointer is not valid JSON") from exc
    if not isinstance(data, dict):
        raise HeartbeatError("active pointer must be a JSON object")
    path_value = str(data.get("path") or "")
    active_path = _is_relative_child(path_value, state_root)
    if not active_path.exists():
        pointer.unlink()
        return None
    return data


def validate_latest_pointer(state_root: Path) -> dict[str, Any] | None:
    pointer = state_root / LATEST_NAME
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HeartbeatError("latest pointer is not valid JSON") from exc
    if not isinstance(data, dict):
        raise HeartbeatError("latest pointer must be a JSON object")
    run_path = _is_relative_child(str(data.get("path") or ""), state_root)
    manifest = run_path / "manifest.json"
    if not manifest.is_file():
        raise HeartbeatError("latest pointer references a run without manifest.json")
    expected_hash = data.get("manifest_sha256")
    if expected_hash and expected_hash != sha256_file(manifest):
        raise HeartbeatError("latest pointer manifest hash mismatch")
    return data


def recover_staged_runs(state_root: Path) -> list[dict[str, Any]]:
    runs = _ensure_state_dirs(state_root)
    recovered: list[dict[str, Any]] = []
    for staged in sorted(runs.glob("*.staged")):
        if not staged.is_dir():
            continue
        manifest_path = staged / "manifest.json"
        manifest: dict[str, Any]
        if manifest_path.is_file():
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                manifest = {}
        else:
            manifest = {}
        manifest.update(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "failed_recovered",
                "recovered_at": utc_now(),
                "recovery_reason": "staged run was present before a new run started",
            }
        )
        atomic_write_json(manifest_path, manifest)
        suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recovered_path = staged.with_name(f"{staged.name.removesuffix('.staged')}.recovered-{suffix}")
        counter = 1
        while recovered_path.exists():
            counter += 1
            recovered_path = staged.with_name(f"{staged.name.removesuffix('.staged')}.recovered-{suffix}-{counter}")
        os.replace(staged, recovered_path)
        recovered.append({"from": staged.name, "to": recovered_path.name, "status": "failed_recovered"})
    active = state_root / ACTIVE_NAME
    if active.exists():
        try:
            validate_active_pointer(state_root)
        except HeartbeatError:
            raise
        if active.exists():
            active.unlink()
    return recovered


def stale_after_seconds(config: dict[str, Any]) -> int:
    heartbeat = config.get("heartbeat") if isinstance(config.get("heartbeat"), dict) else {}
    raw = heartbeat.get("stale_after_seconds", 3600)
    try:
        seconds = int(raw)
    except (TypeError, ValueError) as exc:
        raise HeartbeatError("heartbeat.stale_after_seconds must be an integer") from exc
    if seconds < 1:
        raise HeartbeatError("heartbeat.stale_after_seconds must be at least one second")
    return seconds


def _current_hostname() -> str:
    return os.uname().nodename if hasattr(os, "uname") else ""


def _read_lock_payload(lock_fd: int) -> dict[str, Any]:
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        loaded = json.loads(os.read(lock_fd, 65536).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "unreadable"}
    return loaded if isinstance(loaded, dict) else {"state": "invalid"}


def _is_proven_stale_lock(payload: dict[str, Any], stale_after: int) -> bool:
    created_at = payload.get("created_at")
    hostname = payload.get("hostname")
    pid = payload.get("pid")
    if not isinstance(created_at, str) or not isinstance(hostname, str) or hostname != _current_hostname():
        return False
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if created.tzinfo is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
    if age_seconds < stale_after:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _write_lock_payload(lock_fd: int, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if os.write(lock_fd, encoded) != len(encoded):
        raise HeartbeatError("unable to write complete heartbeat lock payload")
    os.fsync(lock_fd)


def acquire_lock(state_root: Path, stale_after: int) -> tuple[int | None, dict[str, Any]]:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / LOCK_NAME
    payload = {"created_at": utc_now(), "hostname": _current_hostname(), "pid": os.getpid()}
    unready_retries = 0
    while True:
        try:
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing_fd = os.open(lock_path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(existing_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                existing = _read_lock_payload(existing_fd)
                os.close(existing_fd)
                return None, existing
            retry_unready = False
            try:
                existing = _read_lock_payload(existing_fd)
                try:
                    current_stat = lock_path.stat()
                except FileNotFoundError:
                    continue
                if not os.path.samestat(os.fstat(existing_fd), current_stat):
                    continue
                if existing.get("state") in {"unreadable", "invalid"}:
                    if unready_retries >= 1:
                        return None, {"state": "acquiring"}
                    unready_retries += 1
                    retry_unready = True
                elif not _is_proven_stale_lock(existing, stale_after):
                    return None, existing
                else:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        continue
            finally:
                fcntl.flock(existing_fd, fcntl.LOCK_UN)
                os.close(existing_fd)
            if retry_unready:
                continue
            continue
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write_lock_payload(lock_fd, payload)
        except Exception:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return lock_fd, payload


def release_lock(state_root: Path, lock_fd: int) -> None:
    lock_path = state_root / LOCK_NAME
    try:
        try:
            if os.path.samestat(os.fstat(lock_fd), lock_path.stat()):
                lock_path.unlink()
        except FileNotFoundError:
            pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _staged_artifact_path(staged: Path, name: Any) -> Path:
    if not isinstance(name, str) or not name:
        raise HeartbeatError("staged artifact name must be a non-empty relative path")
    return _is_relative_child(name, staged)


def validate_staged_run(staged: Path) -> dict[str, Any]:
    if not staged.is_dir():
        raise HeartbeatError(f"staged run is not a directory: {staged}")
    manifest_path = staged / "manifest.json"
    if not manifest_path.is_file():
        raise HeartbeatError("staged run missing manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HeartbeatError("staged manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise HeartbeatError("staged manifest must be an object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise HeartbeatError("staged manifest schema_version mismatch")
    if manifest.get("status") not in {"succeeded", "failed", "skipped"}:
        raise HeartbeatError("staged manifest status is not final")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise HeartbeatError("staged manifest must include artifact hashes")
    for required in ("heartbeat-result.json", "command-log.json"):
        if required not in artifacts:
            raise HeartbeatError(f"staged manifest missing required artifact: {required}")
    for name, expected in artifacts.items():
        if not isinstance(expected, str):
            raise HeartbeatError(f"staged artifact hash must be a string: {name}")
        path = _staged_artifact_path(staged, name)
        if not path.is_file():
            raise HeartbeatError(f"staged artifact missing: {name}")
        if sha256_file(path) != expected:
            raise HeartbeatError(f"staged artifact hash mismatch: {name}")
    command_log_path = _staged_artifact_path(staged, "command-log.json")
    try:
        command_log = json.loads(command_log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HeartbeatError("staged command log is not valid JSON") from exc
    commands = command_log.get("commands") if isinstance(command_log, dict) else None
    if not isinstance(commands, list):
        raise HeartbeatError("staged command log must contain a commands list")
    for index, entry in enumerate(commands, start=1):
        if not isinstance(entry, dict):
            raise HeartbeatError(f"staged command log entry {index} must be an object")
        sidecar = entry.get("sidecar")
        sidecar_hash = entry.get("sidecar_sha256")
        if not isinstance(sidecar_hash, str) or artifacts.get(sidecar) != sidecar_hash:
            raise HeartbeatError(f"staged command sidecar is not committed: {sidecar}")
        if sha256_file(_staged_artifact_path(staged, sidecar)) != sidecar_hash:
            raise HeartbeatError(f"staged command sidecar hash mismatch: {sidecar}")
    return manifest


def promote_staged_run(staged: Path, state_root: Path) -> dict[str, Any]:
    manifest = validate_staged_run(staged)
    run_id = str(manifest.get("run_id") or staged.name.removesuffix(".staged"))
    final = state_root / RUNS_DIR_NAME / run_id
    if final.exists():
        raise HeartbeatError(f"run already exists: {run_id}")
    os.replace(staged, final)
    manifest_hash = sha256_file(final / "manifest.json")
    latest = {
        "run_id": run_id,
        "path": f"{RUNS_DIR_NAME}/{run_id}",
        "status": manifest.get("status"),
        "manifest_sha256": manifest_hash,
        "updated_at": utc_now(),
    }
    atomic_write_json(state_root / LATEST_NAME, latest)
    return latest


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"


def _write_active_pointer(state_root: Path, staged: Path, run_id: str) -> None:
    rel = staged.relative_to(state_root).as_posix()
    atomic_write_json(state_root / ACTIVE_NAME, {"run_id": run_id, "path": rel, "created_at": utc_now()})


def _finalize_staged(
    staged: Path,
    *,
    manifest: dict[str, Any],
    result: dict[str, Any],
    command_log: dict[str, Any],
) -> dict[str, Any]:
    commands = command_log.get("commands")
    if not isinstance(commands, list):
        raise HeartbeatError("command log must contain a commands list")
    atomic_write_json(staged / "heartbeat-result.json", result)
    atomic_write_json(staged / "command-log.json", command_log)
    artifacts = {
        "heartbeat-result.json": sha256_file(staged / "heartbeat-result.json"),
        "command-log.json": sha256_file(staged / "command-log.json"),
    }
    for index, entry in enumerate(commands, start=1):
        if not isinstance(entry, dict):
            raise HeartbeatError(f"command log entry {index} must be an object")
        sidecar = entry.get("sidecar")
        sidecar_hash = entry.get("sidecar_sha256")
        sidecar_path = _staged_artifact_path(staged, sidecar)
        if not isinstance(sidecar_hash, str) or not sidecar_path.is_file():
            raise HeartbeatError(f"command sidecar is incomplete: {sidecar}")
        if sha256_file(sidecar_path) != sidecar_hash:
            raise HeartbeatError(f"command sidecar hash mismatch: {sidecar}")
        artifacts[sidecar] = sidecar_hash
    manifest["artifacts"] = artifacts
    atomic_write_json(staged / "manifest.json", manifest)
    return manifest


def run_once(
    *,
    target_root: Path,
    config_path: Path | None = None,
    no_agent: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    target_root = target_root.resolve()
    config_path = config_path or config_path_for(target_root)
    config = load_yaml(config_path)
    state_root = state_root_from_config(target_root, config)
    if not heartbeat_enabled(config) and not force:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "heartbeat.disabled",
            "target_root": str(target_root),
            "config_path": str(config_path),
        }
    lock_fd, lock_payload = acquire_lock(state_root, stale_after_seconds(config))
    if lock_fd is None:
        return {"ok": False, "status": "locked", "lock": lock_payload, "target_root": str(target_root)}
    staged: Path | None = None
    run_id = ""
    command_entries: list[dict[str, Any]] = []
    try:
        recovered = recover_staged_runs(state_root)
        validate_active_pointer(state_root)
        run_id = _new_run_id()
        staged = state_root / RUNS_DIR_NAME / f"{run_id}.staged"
        staged.mkdir(parents=True, exist_ok=False)
        _write_active_pointer(state_root, staged, run_id)
        config_hash = sha256_json(config)
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": "running",
            "started_at": utc_now(),
            "target_root": str(target_root),
            "config_path": str(config_path),
            "config_sha256": config_hash,
            "pid": os.getpid(),
            "sid": os.getsid(0) if hasattr(os, "getsid") else None,
            "pgid": os.getpgrp() if hasattr(os, "getpgrp") else None,
            "recovered_before_run": recovered,
        }
        atomic_write_json(staged / "manifest.json", manifest)
        planned = planned_commands(config, no_agent=no_agent, target_root=target_root)

        status = "succeeded"
        for index, command in enumerate(planned, start=1):
            argv = command["command"]
            sidecar = staged / f"command-{index:02d}.json"
            try:
                validate_direct_command(command.get("policy_command", argv), config, allow_direct=bool(command.get("allow_direct")))
            except HeartbeatError as exc:
                launcher_info = command.get("launcher_info") if isinstance(command.get("launcher_info"), dict) else {}
                entry = write_command_sidecar(
                    sidecar,
                    {
                        "id": command["id"],
                        "argv": argv,
                        "cwd": str(target_root),
                        "started_at": utc_now(),
                        "timeout_seconds": int(command["timeout_seconds"]),
                        **launcher_info,
                        "blocked": True,
                        "returncode": None,
                        "timed_out": False,
                        "stdout_tail": "",
                        "stderr_tail": "",
                        "error": str(exc),
                        "finished_at": utc_now(),
                    },
                )
                command_entries.append(entry)
                status = "failed"
                break
            entry = run_command(
                str(command["id"]),
                argv,
                cwd=target_root,
                timeout_seconds=int(command["timeout_seconds"]),
                sidecar_path=sidecar,
                launcher_info=command.get("launcher_info") if isinstance(command.get("launcher_info"), dict) else None,
                stdin_text=command.get("stdin_text") if isinstance(command.get("stdin_text"), str) else None,
                protocol_options=command.get("protocol_options"),
            )
            command_entries.append(entry)
            if entry.get("returncode") != 0:
                status = "failed"
                break

        result = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "status": status,
            "target_root": str(target_root),
            "no_agent": no_agent,
            "command_count": len(command_entries),
            "recovered_before_run": recovered,
            "finished_at": utc_now(),
        }
        command_log = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "commands": command_entries}
        manifest.update({"status": status, "finished_at": utc_now()})
        manifest = _finalize_staged(staged, manifest=manifest, result=result, command_log=command_log)
        latest = promote_staged_run(staged, state_root)
        active = state_root / ACTIVE_NAME
        if active.exists():
            active.unlink()
        return {
            "ok": status == "succeeded",
            "status": status,
            "run_id": run_id,
            "latest": latest,
            "state_root": str(state_root),
            "manifest": manifest,
            "result": result,
        }
    except Exception:
        if staged is not None and staged.exists() and staged.name.endswith(".staged"):
            try:
                manifest_path = staged / "manifest.json"
                manifest = {}
                if manifest_path.exists():
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = loaded if isinstance(loaded, dict) else {}
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "status": "failed",
                    "target_root": str(target_root),
                    "no_agent": no_agent,
                    "command_count": len(command_entries),
                    "finished_at": utc_now(),
                }
                command_log = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "commands": command_entries}
                manifest.update({"schema_version": SCHEMA_VERSION, "run_id": run_id, "status": "failed", "finished_at": utc_now()})
                _finalize_staged(staged, manifest=manifest, result=result, command_log=command_log)
                promote_staged_run(staged, state_root)
            except Exception:
                pass
        raise
    finally:
        release_lock(state_root, lock_fd)


def status(*, target_root: Path, config_path: Path | None = None) -> dict[str, Any]:
    target_root = target_root.resolve()
    config_path = config_path or config_path_for(target_root)
    payload: dict[str, Any] = {
        "ok": True,
        "target_root": str(target_root),
        "config_path": str(config_path),
        "config_exists": config_path.is_file(),
    }
    if not config_path.is_file():
        payload.update({"enabled": False, "state_exists": False})
        return payload
    config = load_yaml(config_path)
    state_root = state_root_from_config(target_root, config)
    payload.update(
        {
            "enabled": heartbeat_enabled(config),
            "state_root": str(state_root),
            "state_exists": state_root.exists(),
            "locked": (state_root / LOCK_NAME).exists(),
        }
    )
    try:
        payload["latest"] = validate_latest_pointer(state_root) if state_root.exists() else None
        payload["active"] = validate_active_pointer(state_root) if state_root.exists() else None
    except HeartbeatError as exc:
        payload["ok"] = False
        payload.setdefault("issues", []).append(str(exc))
    return payload
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the generic agent heartbeat harness.")
    parser.add_argument("--target-root", default=".", help="Target repository root.")
    parser.add_argument("--config", default=None, help="Path to config/codex_heartbeat.yaml.")
    parser.add_argument("--no-agent", action="store_true", help="Skip the configured agent command and exercise transactions only.")
    parser.add_argument("--force", action="store_true", help="Run even when heartbeat.enabled is false.")
    parser.add_argument("--status", action="store_true", help="Report status instead of running.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        target = Path(args.target_root).expanduser().resolve()
        config = Path(args.config).expanduser().resolve() if args.config else None
        payload = status(target_root=target, config_path=config) if args.status else run_once(
            target_root=target,
            config_path=config,
            no_agent=args.no_agent,
            force=args.force,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
