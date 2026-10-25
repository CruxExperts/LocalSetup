"""Kernel-enforced, process-tree containment for local and CI test runs."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

from .test_workers import default_test_workers, testing_configuration


def cgroup_directory() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::/"):
            return Path("/sys/fs/cgroup") / line[4:]
    raise RuntimeError("test safety requires Linux cgroup v2")


def verified_limits() -> dict[str, int]:
    """Read actual kernel controls, never trust an environment marker."""
    directory = cgroup_directory()
    if not directory.name.startswith("localsetup-tests-"):
        raise RuntimeError("tests are outside a LocalSetup resource boundary")
    memory = int((directory / "memory.max").read_text())
    swap = int((directory / "memory.swap.max").read_text())
    tasks = int((directory / "pids.max").read_text())
    quota, period = map(int, (directory / "cpu.max").read_text().split())
    if min(memory, tasks, quota, period) <= 0 or swap != 0:
        raise RuntimeError("test resource limits are not effective")
    if (directory / "memory.oom.group").read_text().strip() != "1":
        raise RuntimeError("test OOM group termination is not effective")
    return {"memory_bytes": memory, "swap_bytes": swap, "tasks": tasks,
            "cpu_quota_percent": quota * 100 // period}


def resource_budget(project_root: Path | None = None) -> dict[str, int]:
    """Leave half the host RAM and CPUs available to interactive workloads."""
    policy = testing_configuration(project_root or Path.cwd()) or {}
    fraction = policy.get("memory_percent", 50)
    values = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith(("MemTotal:", "MemAvailable:"))}
    memory = min(values["MemTotal"], values["MemAvailable"]) * fraction // 100
    cpu_percent = default_test_workers() * 100
    # Respect tighter enclosing cgroups, including hosted/container runners.
    directory = cgroup_directory()
    while directory != Path("/sys/fs"):
        limit_file = directory / "memory.max"
        if limit_file.is_file():
            raw = limit_file.read_text().strip()
            if raw != "max":
                current = int((directory / "memory.current").read_text())
                memory = min(memory, max(0, (int(raw) - current) * fraction // 100))
        cpu_file = directory / "cpu.max"
        if cpu_file.is_file():
            quota, period = cpu_file.read_text().split()
            if quota != "max":
                cpu_percent = min(cpu_percent, max(1, int(quota) * 100 // int(period) // 3))
        if directory == Path("/sys/fs/cgroup"):
            break
        if directory.parent == directory:
            break
        directory = directory.parent
    if memory < 512 * 1024 * 1024:
        raise RuntimeError("insufficient free memory for a protected test run (512 MiB minimum)")
    return {"memory_bytes": memory, "cpu_quota_percent": cpu_percent,
            "tasks": policy.get("tasks_max", 1024)}


def run_protected(command: list[str], *, project_root: Path | None = None) -> int:
    """Serialize independent runs; descendants share one verified kernel budget."""
    try:
        verified_limits()
    except (OSError, ValueError, RuntimeError):
        pass
    else:
        return subprocess.call(command)
    if sys.platform != "linux":
        raise RuntimeError("protected test execution requires Linux cgroup v2 and a systemd user manager")
    runtime = Path(f"/run/user/{os.getuid()}")
    if runtime.stat().st_uid != os.getuid():
        raise RuntimeError("test safety runtime directory has an unexpected owner")
    # Keep the lock outside the repository so concurrent checkouts share it.
    fd = os.open(runtime / "localsetup-tests.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another protected test run is active; do not overlap test budgets") from exc
        active = subprocess.run(["systemctl", "--user", "list-units", "--state=active,activating,deactivating,reloading",
                                 "--no-legend", "--plain", "localsetup-tests-*.scope"],
                                capture_output=True, text=True, timeout=10, check=True)
        if active.stdout.strip():
            raise RuntimeError("a prior test scope remains active; inspect and stop that owned run before retrying")
        budget = resource_budget(project_root)
        unit = f"localsetup-tests-{uuid.uuid4().hex}.scope"
        print("LocalSetup test safety: " + json.dumps({"unit": unit, **budget}), flush=True)
        argv = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={unit}",
                "-p", f"MemoryMax={budget['memory_bytes']}",
                "-p", f"MemoryHigh={budget['memory_bytes'] * 9 // 10}",
                "-p", "MemorySwapMax=0", "-p", "OOMPolicy=kill",
                "-p", f"CPUQuota={budget['cpu_quota_percent']}%",
                "-p", f"TasksMax={budget['tasks']}",
                "-p", "RuntimeMaxSec=7200",
                "-p", "TimeoutStopSec=10",
                sys.executable, "-m", "ls.core.test_resources", "--inside", *command]
        def terminate(_signum, _frame):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            return subprocess.call(argv)
        finally:
            # A detached fixture can survive pytest; stop only our unique unit.
            stopped = subprocess.run(["systemctl", "--user", "stop", unit],
                                     capture_output=True, text=True, timeout=20)
            if stopped.returncode and "not loaded" not in stopped.stderr:
                raise RuntimeError(f"test safety cleanup pending: {unit}: {stopped.stderr.strip()}")
            signal.signal(signal.SIGTERM, previous)


def main() -> int:
    args = sys.argv[1:]
    try:
        if args and args[0] == "--inside":
            limits = verified_limits()
            print("LocalSetup verified test limits: " + json.dumps(limits), flush=True)
            if len(args) < 2:
                raise ValueError("missing protected command")
            os.execvp(args[1], args[1:])
        project_root = None
        if args and args[0] == "--project-root":
            if len(args) < 3:
                raise ValueError("--project-root requires a path and command")
            project_root = Path(args[1]).resolve(strict=True)
            args = args[2:]
        if args and args[0] == "--":
            args = args[1:]
        if not args:
            raise ValueError("usage: python -m ls.core.test_resources -- COMMAND [ARG ...]")
        return run_protected(args, project_root=project_root)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"LocalSetup test safety refused execution: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
