"""Run-bound test telemetry. The outer supervisor is the sole snapshot writer."""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
import json
import os
from pathlib import Path
import stat
import time

from .lockfile import save_json
from .terminal_ops.command_state import process_info
from .framework_version import framework_version


MAX_BYTES = 262144
TERMINAL_STATES = {"completed", "failed", "interrupted"}


@lru_cache(maxsize=2)
def validator(progress=False):
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[1] / "config/test-telemetry.schema.json").read_text())
    if progress:
        schema = {"$ref": "#/$defs/progress", "$defs": schema["$defs"]}
    return Draft202012Validator(schema)


def runtime_root() -> Path:
    path = Path(f"/run/user/{os.getuid()}")
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise RuntimeError("test telemetry runtime directory is not private and owned")
    return path


def status_path():
    return runtime_root() / "localsetup-tests-status.json"


def progress_path():
    return runtime_root() / "localsetup-tests-progress.json"


def read_record(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
            raise ValueError("invalid test telemetry record ownership, mode or size")
        raw = stream.read(MAX_BYTES + 1)
    from .application_profiles.service import _bounded_json
    value = _bounded_json(raw, label="test telemetry", limit=MAX_BYTES)
    if not isinstance(value, dict):
        raise ValueError("test telemetry must be an object")
    return value


def controller_binding():
    """Only the original exec PID publishes; workers and nested runs stay separate."""
    try:
        pid = int(os.environ.get("LOCALSETUP_TEST_CONTROLLER_PID", "0"))
        start = int(os.environ.get("LOCALSETUP_TEST_CONTROLLER_START", "0"))
    except ValueError:
        return None
    current = process_info(os.getpid())
    if pid != os.getpid() or not current or current["starttime"] != start:
        return None
    from .test_resources import cgroup_directory
    unit = cgroup_directory().name
    if unit != os.environ.get("LOCALSETUP_TEST_RUN_ID"):
        return None
    return {"run_id": unit, "pid": pid, "starttime": start}


def initialize_controller(unit):
    snapshot = read_record(status_path())
    if (snapshot.get("run_id") != unit or snapshot.get("state") not in {"starting", "running"}
            or snapshot.get("controller") is not None):
        raise RuntimeError("test telemetry bootstrap differs from its owned scope")
    if (os.environ.get("LOCALSETUP_TEST_RUN_ID") == unit
            and os.environ.get("LOCALSETUP_TEST_CONTROLLER_PID") != str(os.getpid())):
        raise RuntimeError("nested launchers cannot replace the test telemetry controller")
    current = process_info(os.getpid())
    if current is None:
        raise RuntimeError("test controller process identity is unavailable")
    os.environ["LOCALSETUP_TEST_RUN_ID"] = unit
    os.environ["LOCALSETUP_TEST_CONTROLLER_PID"] = str(os.getpid())
    os.environ["LOCALSETUP_TEST_CONTROLLER_START"] = str(current["starttime"])
    publish_progress({})


def publish_progress(progress):
    binding = controller_binding()
    if binding is None:
        return False
    value = {"schema_version": 1, **binding, "updated_at": time.time(), "progress": progress}
    if len(json.dumps(value).encode()) > MAX_BYTES:
        raise ValueError("test progress exceeds its bounded record size")
    save_json(progress_path(), value)
    return True


class Telemetry:
    def __init__(self, unit, project, settings, maximum, *, requested_maximum=None):
        self.data = {
            "schema_version": 1, "run_id": unit, "project": str(project.resolve()),
            "framework_version": framework_version(),
            "supervisor": process_info(os.getpid()), "controller": None,
            "sequence": 0, "started_at": time.time(), "updated_at": time.time(),
            "stale_after_seconds": max(30, 3 * min(settings.get("telemetry_interval_seconds", 5),
                settings.get("adaptive", {}).get("sample_seconds", 1))),
            "state": "starting", "exit_code": None, "cleanup": "pending",
            "resources": None, "resources_sampled_at": None,
            "progress": None, "progress_updated_at": None,
            "limits": {"configured_workers": settings.get("workers", "auto"),
                       "requested_maximum": maximum if requested_maximum is None else requested_maximum,
                       "effective_maximum": maximum,
                       "minimum": min(maximum, settings.get("adaptive", {}).get("min_workers", 1))},
            "watchdog": {"adaptive": maximum > 1 and settings.get("adaptive", {}).get("enabled", True),
                         "reason": "starting"},
        }
        self.write()

    def merge_progress(self, *, allow_exited=False):
        try:
            record = read_record(progress_path())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.data["progress_error"] = "invalid_progress_record"
            return
        binding = {key: record.get(key) for key in ("pid", "starttime")}
        if record.get("schema_version") != 1 or record.get("run_id") != self.data["run_id"]:
            return
        if self.data["controller"] is None:
            current = process_info(binding["pid"])
            if type(binding["pid"]) is not int or type(binding["starttime"]) is not int:
                return
            if current is None and not allow_exited:
                return
            if current is not None:
                if current["starttime"] != binding["starttime"]:
                    return
                try:
                    group = Path(f"/proc/{binding['pid']}/cgroup").read_text()
                except FileNotFoundError:
                    return
                if not any(line.endswith("/" + self.data["run_id"]) for line in group.splitlines()):
                    return
            self.data["controller"] = binding
        if binding != self.data["controller"] or not isinstance(record.get("progress"), dict):
            return
        if (not validator(True).is_valid(record["progress"])
                or type(record.get("updated_at")) not in {int, float}):
            self.data["progress_error"] = "invalid_progress_record"
            return
        self.data.pop("progress_error", None)
        self.data["progress"] = record["progress"]
        self.data["progress_updated_at"] = record["updated_at"]
        maximum = (record["progress"].get("workers") or {}).get("maximum")
        if type(maximum) is int and 1 <= maximum <= self.data["limits"]["requested_maximum"]:
            self.data["limits"]["effective_maximum"] = maximum
            minimum = record["progress"]["workers"].get("minimum", 1)
            self.data["limits"]["minimum"] = min(maximum, max(1, minimum))

    def sample(self, directory, sample, reason):
        self.merge_progress()
        resources = {**asdict(sample), "pressure_percent": sample.percent,
                     "headroom_bytes": max(0, sample.window_bytes - sample.working_bytes),
                     "inactive_file_bytes": sample.charged_bytes - sample.working_bytes,
                     "tasks": int((directory / "pids.current").read_text()),
                     "tasks_max": int((directory / "pids.max").read_text())}
        self.data.update(state="running", resources=resources, resources_sampled_at=time.time())
        self.data["watchdog"]["reason"] = reason
        self.write()

    def finish(self, exit_code=None, *, interrupted=False, cleanup="pending"):
        self.merge_progress(allow_exited=True)
        self.data.update(state="interrupted" if interrupted else "completed" if exit_code == 0 else "failed",
                         exit_code=exit_code, cleanup=cleanup)
        self.write()

    def cleaned(self):
        self.data["cleanup"] = "complete"
        try:
            if read_record(progress_path()).get("run_id") == self.data["run_id"]:
                progress_path().unlink()
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            # The input channel is observational. Retain an unverified object
            # rather than delete it or turn a completed workload into a failure.
            self.data["progress_error"] = "invalid_progress_record"
            self.data["progress_cleanup"] = "retained_invalid"
        self.write()

    def write(self):
        self.data["sequence"] += 1
        self.data["updated_at"] = time.time()
        save_json(status_path(), self.data)


def status(project):
    try:
        value = read_record(status_path())
    except FileNotFoundError:
        return {"ok": False, "status": "missing"}
    if value.get("schema_version") != 1:
        raise ValueError("unsupported test telemetry schema")
    if not validator().is_valid(value):
        raise ValueError("invalid test telemetry snapshot")
    if value.get("project") != str(project.resolve()):
        return {"ok": False, "status": "different_project", "run_id": value.get("run_id")}
    state = "finished" if value["state"] in TERMINAL_STATES and value["cleanup"] == "complete" else "live"
    owner = value.get("supervisor") or {}
    current = process_info(owner.get("pid"))
    if state == "live" and (current is None or current["starttime"] != owner.get("starttime")
                            or time.time() - value["updated_at"] > value.get("stale_after_seconds", 10)):
        state = "stale"
    return {"ok": state != "stale", "status": state, "snapshot": value}


def render(value, *, color=False, width=80):
    from .branding.terminal import render_parts
    resources, progress = value.get("resources") or {}, value.get("progress") or {}
    workers = progress.get("workers") or {}
    def gib(key):
        number = resources.get(key)
        return "?" if number is None else f"{number / 1024**3:.1f}"
    maximum = workers.get("maximum", value["limits"]["effective_maximum"])
    closed = value["cleanup"] == "complete"
    state = "finishing" if value["state"] in TERMINAL_STATES and not closed else value["state"]
    total = progress.get("total")
    role = {"completed": "success", "failed": "error", "interrupted": "warning", "stale": "warning"}.get(state, "value")
    parts = [("[", "brand"), ("LS", "value"), ("]", "brand"),
             (" v" + value.get("framework_version", "?"), "value"), (" tests ", "label"), (state, role)]
    fields = [
        ("wrk", f"{0 if closed else workers.get('active', '?')}/{maximum}"),
        ("tgt", str(workers.get("target", "?"))),
        ("done", f"{progress.get('completed', '?')}/{total if total is not None else '?'}"),
    ]
    for label, number in fields:
        parts.extend([(" | " + label + " ", "label"), (number, "value")])
    memory = [("RAM ", "label"), (f"{gib('working_bytes')}G work", "value"),
              (" | used ", "label"), (f"{gib('charged_bytes')}/{gib('limit_bytes')}G", "value"),
              (" | OS thr ", "label"), (f"{resources.get('tasks', '?')}/{resources.get('tasks_max', '?')}", "value")]
    if closed:
        memory.append((" [last sample]", "label"))
    width = 80 if width is None else min(80, max(0, width))
    return "\n".join(render_parts(row, color=color, width=width, pad=True) for row in (parts, memory))
