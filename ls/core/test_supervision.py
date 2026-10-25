"""Outer memory watchdog: independent of pytest and limited to its owned scope."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

from .test_pressure import AdaptivePolicy, PressureController, sample_pressure


def supervise(argv: list[str], unit: str, settings: dict, maximum: int, *, telemetry=None, display=True) -> int:
    policy = AdaptivePolicy.parse(settings.get("adaptive"))
    controller = PressureController(maximum, policy) if maximum > 1 and policy.enabled else None
    interval = min(policy.sample_seconds, settings.get("telemetry_interval_seconds", 5))
    display_at = 0
    directory = None
    started = time.monotonic()
    process = subprocess.Popen(argv)
    try:
        while True:
            try:
                result = process.wait(timeout=interval)
                if telemetry is not None:
                    telemetry.finish(result, interrupted=result < 0 or result in {130, 143})
                return result
            except subprocess.TimeoutExpired:
                pass
            if directory is None:
                result = subprocess.run(["systemctl", "--user", "show", unit,
                                         "--property=ControlGroup", "--value"],
                                        capture_output=True, text=True, timeout=5)
                relative = result.stdout.strip()
                if not relative:
                    if time.monotonic() - started < 10:
                        continue
                    raise RuntimeError("test watchdog could not resolve its owned resource boundary")
                directory = Path("/sys/fs/cgroup") / relative.lstrip("/")
                if directory.name != unit or ".." in directory.parts:
                    raise RuntimeError("test watchdog scope identity differs")
            try:
                sample = sample_pressure(directory, settings)
            except FileNotFoundError:
                # The scope can disappear between a wait timeout and the read.
                result = process.poll()
                if result is not None:
                    if telemetry is not None:
                        telemetry.finish(result, interrupted=result < 0 or result in {130, 143})
                    return result
                raise RuntimeError("test watchdog lost its live memory boundary") from None
            now = time.monotonic()
            decision = controller.observe(sample.percent, now) if controller is not None else None
            if telemetry is not None:
                try:
                    telemetry.sample(directory, sample, (decision.reason or "steady") if decision else "fixed_concurrency")
                except FileNotFoundError:
                    result = process.poll()
                    if result is None:
                        raise RuntimeError("test watchdog lost its live task boundary") from None
                    telemetry.finish(result, interrupted=result < 0 or result in {130, 143})
                    return result
                if display and now >= display_at:
                    from .test_telemetry import render
                    from .branding.terminal import color_capable
                    # Pytest writes activity dots without a trailing newline.
                    # Frame each update so subsequent dots start below it.
                    sys.stdout.write("\n" + render(telemetry.data, color=color_capable(sys.stdout)) + "\n")
                    sys.stdout.flush()
                    display_at = now + settings.get("telemetry_interval_seconds", 5)
            if decision is not None and decision.emergency:
                print("LocalSetup test safety: " + json.dumps({
                    "status": "interrupted_memory_pressure", "unit": unit,
                    "working_bytes": sample.working_bytes, "window_bytes": sample.window_bytes,
                    "charged_bytes": sample.charged_bytes,
                    "message": "Emergency grace expired; terminating only this test scope. Coverage is incomplete.",
                }), flush=True)
                # systemd first sends TERM; the existing TimeoutStopSec supplies
                # the bounded KILL fallback for non-cooperating descendants.
                subprocess.run(["systemctl", "--user", "stop", unit],
                               capture_output=True, timeout=20, check=True)
                process.wait(timeout=10)
                if telemetry is not None:
                    telemetry.finish(4, interrupted=True)
                return 4
    finally:
        if process.poll() is None:
            subprocess.run(["systemctl", "--user", "stop", unit],
                           capture_output=True, timeout=20, check=True)
            process.wait(timeout=10)
        if telemetry is not None and telemetry.data["state"] not in {"completed", "failed", "interrupted"}:
            telemetry.finish(process.poll(), interrupted=True)


def command_worker_limit(command: list[str], resolved: int) -> int:
    """Honor explicit serial pytest without a redundant sampling loop."""
    is_pytest = (bool(command) and Path(command[0]).name in {"pytest", "py.test"}) or any(
        command[index:index + 2] == ["-m", "pytest"] for index in range(len(command) - 1))
    if not is_pytest:
        return resolved
    if any(a in {"--pdb", "--ci-shard"} or a.startswith("--ci-shard=") for a in command):
        return 1
    for index, argument in enumerate(command):
        if argument in {"-n", "--numprocesses"} and index + 1 < len(command):
            value = command[index + 1]
        elif argument.startswith("--numprocesses="):
            value = argument.partition("=")[2]
        elif argument.startswith("-n") and argument[2:].isdigit():
            value = argument[2:]
        elif argument in {"--pdb", "--ci-shard"} or argument.startswith("--ci-shard="):
            return 1
        else:
            continue
        if value.isdigit():
            return max(1, int(value))
    return resolved
