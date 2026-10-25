from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Any


MIN_TEST_WORKERS = 1
TEST_WORKERS_ENV = "LOCALSETUP_TEST_WORKERS"


def available_cpu_count() -> int | None:
    process_count = getattr(os, "process_cpu_count", None)
    if callable(process_count):
        available = process_count()
        if available:
            return available
    affinity = getattr(os, "sched_getaffinity", None)
    if callable(affinity):
        try:
            available = len(affinity(0))
        except OSError:
            available = 0
        if available:
            return available
    return os.cpu_count()


def testing_configuration(project_root: Path | str | None = None) -> dict[str, Any] | None:
    """Read only the selected project's explicit test scheduling policy."""
    path = Path(project_root or Path.cwd()) / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid testing project configuration: {path}") from exc
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError("[tool] must be a table")
    localsetup = tool.get("localsetup", {})
    if not isinstance(localsetup, dict):
        raise ValueError("[tool.localsetup] must be a table")
    testing = localsetup.get("testing")
    if testing is None:
        return None
    if not isinstance(testing, dict) or set(testing) - {"workers", "shard_count", "memory_percent", "tasks_max", "worker_memory_mib"}:
        raise ValueError("[tool.localsetup.testing] has unsupported fields")
    workers = testing.get("workers", "auto")
    shards = testing.get("shard_count", 1)
    if (workers != "auto" and (type(workers) is not int or workers < 1)):
        raise ValueError("testing workers must be 'auto' or a positive integer")
    if type(shards) is not int or not 1 <= shards <= 32:
        raise ValueError("testing shard_count must be between 1 and 32")
    for name, default, low, high in (("memory_percent", 50, 1, 50),
                                    ("tasks_max", 1024, 32, 4096),
                                    ("worker_memory_mib", 1024, 256, 65536)):
        value = testing.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"testing {name} must be between {low} and {high}")
    return {"workers": workers, "shard_count": shards,
            "memory_percent": testing.get("memory_percent", 50),
            "tasks_max": testing.get("tasks_max", 1024),
            "worker_memory_mib": testing.get("worker_memory_mib", 1024)}


def effective_max_test_workers(cpu_count: int | None = None) -> int:
    """Hard override ceiling: no more than the process's available logical CPUs."""
    available = cpu_count if cpu_count is not None else available_cpu_count()
    return max(MIN_TEST_WORKERS, available or MIN_TEST_WORKERS)


def clamp_test_workers(value: int, *, cpu_count: int | None = None) -> int:
    return min(effective_max_test_workers(cpu_count), max(MIN_TEST_WORKERS, value))


def default_test_workers(cpu_count: int | None = None) -> int:
    available = effective_max_test_workers(cpu_count)
    return max(MIN_TEST_WORKERS, available // 3)


def parse_test_worker_override(
    value: str, *, field: str = "test workers", cpu_count: int | None = None
) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must be an integer")
    try:
        parsed = int(stripped, 10)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    return clamp_test_workers(parsed, cpu_count=cpu_count)


def resolved_test_workers(
    override: str | int | None = None, *, cpu_count: int | None = None,
    env: dict[str, str] | None = None, project_root: Path | str | None = None,
) -> int:
    if override is not None:
        if isinstance(override, int):
            return clamp_test_workers(override, cpu_count=cpu_count)
        return parse_test_worker_override(str(override), field="--workers", cpu_count=cpu_count)
    environ = env if env is not None else os.environ
    env_value = environ.get(TEST_WORKERS_ENV)
    if env_value is not None:
        return parse_test_worker_override(env_value, field=TEST_WORKERS_ENV, cpu_count=cpu_count)
    configured = testing_configuration(project_root)
    if configured is not None and configured["workers"] != "auto":
        return clamp_test_workers(configured["workers"], cpu_count=cpu_count)
    return default_test_workers(cpu_count)


def test_workers_payload(
    override: str | int | None = None, *, cpu_count: int | None = None,
    env: dict[str, str] | None = None, project_root: Path | str | None = None,
) -> dict[str, Any]:
    available = cpu_count if cpu_count is not None else available_cpu_count()
    configured = testing_configuration(project_root)
    workers = resolved_test_workers(override, cpu_count=cpu_count, env=env,
                                    project_root=project_root)
    shard_count = configured["shard_count"] if configured is not None else None
    environ = env if env is not None else os.environ
    worker_source = ("command_line" if override is not None else
                     "environment" if TEST_WORKERS_ENV in environ else
                     "project_configuration" if configured is not None and configured["workers"] != "auto" else
                     "automatic_cpu_formula")
    return {
        "ok": True,
        "workers": workers,
        "worker_source": worker_source,
        "available_cpu_cores": available,
        "default_formula": "floor(available_cpu_cores / 3), minimum 1",
        "min_workers": MIN_TEST_WORKERS,
        "max_workers": effective_max_test_workers(cpu_count),
        "override_env": TEST_WORKERS_ENV,
        "configured_workers": configured["workers"] if configured is not None else None,
        "shard_count": shard_count,
        "shard_indices": list(range(shard_count)) if shard_count is not None else [],
        "workers_per_shard": 1 if shard_count is not None else None,
        "max_parallel": min(workers, shard_count) if shard_count is not None else workers,
    }
