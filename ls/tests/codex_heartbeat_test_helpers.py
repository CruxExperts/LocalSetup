from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls" / "skills" / "ls-codex-heartbeat-checks" / "scripts" / "codex_heartbeat.py"


def load_runtime():
    spec = importlib.util.spec_from_file_location("codex_heartbeat_test_runtime", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(
    target: Path,
    *,
    enabled: bool = True,
    task_queue_path: str | None = None,
    hooks: dict | None = None,
    agent: dict | None = None,
    agent_profiles: dict | None = None,
) -> Path:
    config = {
        "heartbeat": {
            "enabled": enabled,
            "interval_minutes": 15,
            "state_dir": ".localsetup/state/codex-heartbeat",
            "stale_after_seconds": 3600,
            "task_queue_path": task_queue_path,
        },
        "agent": {"enabled": False},
        "hooks": hooks or {"before": [], "after": []},
        "direct_command_policy": {
            "allow_git_writes": False,
            "allow_destructive": False,
            "allowlist": [],
        },
    }
    if agent is not None:
        config["agent"] = agent
    if agent_profiles is not None:
        config["agent_profiles"] = agent_profiles
    path = target / "config" / "codex_heartbeat.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def state_root(target: Path) -> Path:
    return target / ".localsetup" / "state" / "codex-heartbeat"
