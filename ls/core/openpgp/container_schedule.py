"""Fixed, bounded metadata reconciliation for the shared durable scheduler."""
from dataclasses import replace
import math
from pathlib import Path
import time

from ..agent.runtime_lock import runtime_use
from .container_access import _binding, resolve_catalog
from .container_preparation import _Budget
from .container_reader import ContainerReadError
from . import container_runtime as runtime
from .container_live_configuration import ContainerConfiguration, binding_digest


def schedule_id(root: Path) -> str:
    return "container-" + runtime._root(root).name


def bind_job(identity, job):
    """Bind targets, policy, identity and interval without copying secret values."""
    if not isinstance(job, dict) or set(job) != {"runtime_root"}:
        raise ValueError("Invalid indexing job")
    root = runtime._root(Path(job["runtime_root"]))
    if identity != schedule_id(root):
        raise ValueError("Indexing job identity mismatch")
    config = runtime.load_runtime_config(root)
    if not config.enabled or config.read_policy is None or config.remote_content is None:
        raise ContainerReadError("unauthorized")
    if config.source_ids and not config.source_directories:
        raise ContainerReadError("unavailable")
    if Path(config.read_policy.boundary) != root / "reads":
        raise ContainerReadError("unauthorized")
    result = {"runtime_root": str(root), "runtime_sha256": _binding(root, config),
              "interval_seconds": config.scan_interval_seconds}
    if binding_digest(root) is not None:
        # Explicit enrollment after binding authorizes the schema-bounded
        # signed interval. Existing unbound enrollments retain exact pins.
        result['configuration_mode'] = 'signed-tuning-v1'
    return result


def _receipt(root):
    value = runtime._read(root / "reconciliation-run.json")
    if value is None:
        return None
    if (set(value) != {"schema_version", "runtime_sha256", "started_at", "next_due", "state"}
            or value["schema_version"] != 1
            or not isinstance(value["runtime_sha256"], str)
            or not runtime._HEX64.fullmatch(value["runtime_sha256"])
            or value["state"] not in {"started", "reconciled", "pending", "failed"}
            or any(type(value[key]) not in (int, float) or not math.isfinite(value[key])
                   or value[key] < 0 for key in ("started_at", "next_due"))
            or value["next_due"] < value["started_at"]):
        raise ContainerReadError("unavailable")
    return value


def run_due(job):
    """One due scan; failures are redacted and uncertain writes stay pending."""
    try:
        root = runtime._root(Path(job["runtime_root"]))
        if bind_job(schedule_id(root), {"runtime_root": str(root)}) != job:
            return {"ok": False, "state": "job_drift"}
        directory = root / "reconciliation-schedule"
        runtime._private_directory(directory, create=True)
        with runtime_use(directory, exclusive=True, timeout=0):
            now = time.time()
            interval = job['interval_seconds']
            if job.get('configuration_mode') == 'signed-tuning-v1':
                interval = ContainerConfiguration(root, runtime.load_runtime_config(root)).config.scan_interval_seconds
            previous = _receipt(root)
            if previous and previous["runtime_sha256"] == job["runtime_sha256"]:
                if now < previous["started_at"]:
                    return {"ok": False, "state": "clock_reversed"}
                next_due = (previous['started_at'] + interval
                            if job.get('configuration_mode') == 'signed-tuning-v1' else previous['next_due'])
                if now < next_due:
                    return {"ok": True, "state": "not_due"}
            # Record admission before scanning; a crash does not cause a tight
            # restart loop. Catalog pending anchors still own write recovery.
            receipt = {"schema_version": 1, "runtime_sha256": job["runtime_sha256"],
                       "started_at": now, "next_due": now + interval,
                       "state": "started"}
            runtime._write(root / "reconciliation-run.json", receipt)
            def current():
                if bind_job(schedule_id(root), {"runtime_root": str(root)}) != job:
                    raise ContainerReadError("changed")
            try:
                service = resolve_catalog(root, deadline=time.monotonic() + 3600, check=current)
                service.reconcile(_Budget(replace(service.reader.limits, cancel_check=lambda: current() or False)))
                current()
                receipt["state"] = "reconciled"
                result = {"ok": True, "state": "reconciled"}
            except ContainerReadError as error:
                receipt["state"] = "pending" if error.code == "pending" else "failed"
                result = {"ok": error.code == "pending", "state": error.code}
            except Exception:
                receipt["state"] = "failed"
                result = {"ok": False, "state": "unavailable"}
            runtime._write(root / "reconciliation-run.json", receipt)
            return result
    except TimeoutError:
        return {"ok": True, "state": "overlap_skipped"}
    except Exception:
        return {"ok": False, "state": "unavailable"}
