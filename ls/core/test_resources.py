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

from .test_workers import default_test_workers, testing_configuration, resolved_test_workers
from .test_pressure import host_memory, host_reserve
from .test_telemetry import Telemetry


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
    """Resolve requested preferences beneath physical and enclosing limits."""
    policy = testing_configuration(project_root or Path.cwd()) or {}
    fraction = policy.get("memory_percent", 50)
    values = host_memory()
    requested = policy.get("memory_limit_mib")
    memory = (requested * 1024**2 if requested is not None else
              min(values["MemTotal"], values["MemAvailable"]) * fraction // 100)
    memory = min(memory, values["MemTotal"] * policy.get("physical_memory_percent", 95) // 100,
                 max(0, values["MemAvailable"] - host_reserve(values, policy)))
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
        settings = testing_configuration(project_root or Path.cwd()) or {}
        print("LocalSetup test safety: " + json.dumps({"unit": unit, **budget,
              "requested_memory_mib": settings.get("memory_limit_mib"),
              "physical_memory_percent": settings.get("physical_memory_percent", 95),
              "host_reserve_percent": settings.get("host_reserve_percent", 5),
              "host_reserve_mib": settings.get("host_reserve_mib", 512)}), flush=True)
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
        from .test_supervision import command_worker_limit
        maximum = command_worker_limit(command, resolved_test_workers(project_root=project_root))
        requested_maximum = maximum
        maximum = min(maximum, max(1, budget['memory_bytes'] // (settings.get('worker_memory_mib', 1024) * 1024**2)))
        telemetry = Telemetry(unit, Path(project_root or Path.cwd()), settings, maximum,
                              requested_maximum=requested_maximum)
        try:
            from .test_supervision import supervise
            pytest_command = any(command[index:index + 2] == ["-m", "pytest"] for index in range(len(command) - 1)) or Path(command[0]).name in {"pytest", "py.test"}
            return supervise(argv, unit, settings, maximum, telemetry=telemetry, display=not pytest_command)
        finally:
            # A detached fixture can survive pytest; stop only our unique unit.
            stopped = subprocess.run(["systemctl", "--user", "stop", unit],
                                     capture_output=True, text=True, timeout=20)
            if stopped.returncode and "not loaded" not in stopped.stderr:
                raise RuntimeError(f"test safety cleanup pending: {unit}: {stopped.stderr.strip()}")
            if telemetry.data["state"] not in {"completed", "failed", "interrupted"}:
                telemetry.finish(interrupted=True)
            telemetry.cleaned()
            signal.signal(signal.SIGTERM, previous)


def main() -> int:
    from .process_identity import set_process_name
    set_process_name()
    args = sys.argv[1:]
    try:
        if args and args[0] == "--inside":
            limits = verified_limits()
            print("LocalSetup verified test limits: " + json.dumps(limits), flush=True)
            if len(args) < 2:
                raise ValueError("missing protected command")
            from .test_telemetry import initialize_controller
            initialize_controller(cgroup_directory().name)
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
