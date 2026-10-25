"""Deterministic memory-pressure policy shared by test supervision and scheduling."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class AdaptivePolicy:
    enabled: bool = True
    min_workers: int = 1
    target_percent: int = 80
    recovery_percent: int = 70
    critical_percent: int = 95
    down_dwell_seconds: int = 5
    recovery_dwell_seconds: int = 30
    cooldown_seconds: int = 15
    emergency_grace_seconds: int = 5
    sample_seconds: int = 1

    @classmethod
    def parse(cls, value=None):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("testing adaptive has unsupported fields")
        policy = cls(**value)
        if type(policy.enabled) is not bool:
            raise ValueError("testing adaptive enabled must be boolean")
        for field in fields(cls):
            item = getattr(policy, field.name)
            if field.name != "enabled" and (type(item) is not int or not 1 <= item <= 3600):
                raise ValueError(f"testing adaptive {field.name} must be a positive bounded integer")
        if not 1 <= policy.recovery_percent < policy.target_percent < policy.critical_percent <= 95:
            raise ValueError("adaptive thresholds require recovery < target < critical <= 95")
        if policy.sample_seconds > min(policy.down_dwell_seconds, policy.emergency_grace_seconds):
            raise ValueError("adaptive sampling must not exceed the shortest safety dwell")
        return policy


@dataclass(frozen=True)
class PressureSample:
    charged_bytes: int
    working_bytes: int
    limit_bytes: int
    window_bytes: int
    host_available_bytes: int
    reserve_bytes: int

    @property
    def percent(self) -> float:
        return self.working_bytes * 100 / max(1, self.window_bytes)


def host_memory() -> dict[str, int]:
    return {line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))}


def host_reserve(memory: dict[str, int], settings: dict) -> int:
    return max(settings.get("host_reserve_mib", 512) * 1024**2,
               memory["MemTotal"] * settings.get("host_reserve_percent", 5) // 100)


def sample_pressure(directory: Path, settings: dict) -> PressureSample:
    """Exclude reclaimable cache, but retain the kernel's total-charge hard cap."""
    charged = int((directory / "memory.current").read_text())
    limit = int((directory / "memory.max").read_text())
    stats = dict(line.split() for line in (directory / "memory.stat").read_text().splitlines())
    working = max(0, charged - min(charged, int(stats.get("inactive_file", "0"))))
    memory = host_memory()
    reserve = host_reserve(memory, settings)
    window = min(limit, memory["MemTotal"] * settings.get("physical_memory_percent", 95) // 100,
                 working + max(0, memory["MemAvailable"] - reserve))
    for parent in directory.parents:
        maximum = parent / "memory.max"
        if not maximum.is_file():
            continue
        raw = maximum.read_text().strip()
        if raw != "max":
            headroom = max(0, int(raw) - int((parent / "memory.current").read_text()))
            window = min(window, working + headroom)
    return PressureSample(charged, working, limit, max(1, window), memory["MemAvailable"], reserve)


@dataclass(frozen=True)
class PressureDecision:
    workers: int
    reason: str | None = None
    emergency: bool = False


class PressureController:
    """Hysteresis plus continuous dwell and cooldown; no random victim selection."""
    def __init__(self, maximum: int, policy: AdaptivePolicy):
        self.maximum = maximum
        self.minimum = min(maximum, policy.min_workers)
        self.target = maximum
        self.policy = policy
        self.high_since = self.low_since = self.critical_since = None
        self.last_change = self.last_sample = None
        self.emergency = False

    def observe(self, percent: float, now: float) -> PressureDecision:
        if self.maximum <= 1 or not self.policy.enabled:
            return PressureDecision(self.maximum)
        if self.last_sample is not None and now < self.last_sample:
            raise ValueError("pressure samples must use monotonic time")
        self.last_sample = now
        p = self.policy
        self.high_since = (now if self.high_since is None else self.high_since) if percent >= p.target_percent else None
        self.low_since = (now if self.low_since is None else self.low_since) if percent < p.recovery_percent else None
        self.critical_since = (now if self.critical_since is None else self.critical_since) if percent >= p.critical_percent else None
        if self.critical_since is not None:
            self.emergency |= now - self.critical_since >= p.emergency_grace_seconds
            if self.target != self.minimum:
                self.target = self.minimum
                self.last_change = now
            return PressureDecision(self.target, "critical_memory_pressure", self.emergency)
        if self.emergency:
            return PressureDecision(self.target, "interrupted_memory_pressure", True)
        if self.last_change is not None and now - self.last_change < p.cooldown_seconds:
            return PressureDecision(self.target)
        if self.high_since is not None and now - self.high_since >= p.down_dwell_seconds and self.target > self.minimum:
            self.target -= 1
            self.last_change = now
            return PressureDecision(self.target, "memory_pressure")
        if self.low_since is not None and now - self.low_since >= p.recovery_dwell_seconds and self.target < self.maximum:
            self.target += 1
            self.last_change = self.low_since = now
            return PressureDecision(self.target, "sustained_memory_recovery")
        return PressureDecision(self.target)
