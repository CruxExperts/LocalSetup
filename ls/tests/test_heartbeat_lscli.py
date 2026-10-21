import json
from pathlib import Path
import sys

import pytest
import yaml

from ls.tests.codex_heartbeat_test_helpers import load_runtime, state_root, write_config

runtime = load_runtime()
import heartbeat_lscli as profile_owner

TEMPLATE = Path(__file__).resolve().parents[1] / "skills/ls-codex-heartbeat-checks/templates/codex_heartbeat.yaml"


def profile():
    return yaml.safe_load(TEMPLATE.read_text())["agent_profiles"]["lscli-heartbeat"]


def agent():
    return {"enabled": True, "profile": "lscli-heartbeat"}


def test_plan_uses_explicit_typed_argv_without_prompt_leak(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_owner, "_resolve", lambda executable, root: ["/protected/python", "-I", "-B"])
    p = profile()
    p["profile"] = "My 日本語 Profile"
    plan = profile_owner.plan(p, agent(), "lscli-heartbeat", tmp_path)
    argv = plan["command"]
    assert p["prompt"] not in repr(argv) and plan["stdin_text"] == p["prompt"]
    assert argv[argv.index("--workspace")+1] == str(tmp_path)
    assert "--profile=" + p["profile"] in argv
    assert argv[argv.index("--request-limit")+1] == "8"
    assert "--prompt-stdin" in argv and argv[argv.index("--format")+1] == "jsonl"
    assert plan["timeout_seconds"] == 320 and not plan["allow_direct"]


@pytest.mark.parametrize("key,value", [
    ("command", ["untrusted"]), ("executable", "relative"), ("grant", "/a/../b"),
    ("request_limit", True), ("tool_limit", 257), ("token_limit", 0),
    ("timeout_seconds", 3601), ("no_progress_seconds", 301), ("output_limit_bytes", 4194305),
    ("prompt", " "), ("profile", ""),
])
def test_invalid_profile_fails_before_registration(tmp_path, monkeypatch, key, value):
    monkeypatch.setattr(profile_owner, "_resolve", lambda *_: pytest.fail("must not resolve"))
    p = profile()
    p[key] = value
    with pytest.raises(ValueError):
        profile_owner.plan(p, agent(), "lscli-heartbeat", tmp_path)


def test_no_agent_never_resolves_typed_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_owner, "_resolve", lambda *_: pytest.fail("must not resolve"))
    write_config(tmp_path, agent=agent(), agent_profiles={"lscli-heartbeat": {"launcher": "lscli", "client": "lscli"}})
    assert runtime.plan_summary(target_root=tmp_path, no_agent=True)["ok"]
    assert runtime.run_once(target_root=tmp_path, no_agent=True)["ok"]


@pytest.mark.parametrize("success", [True, False])
def test_typed_run_requires_receipt_and_redacts_output(tmp_path, monkeypatch, success):
    code = """import json,sys
assert sys.stdin.read()
def emit(n,t,d): print(json.dumps(dict(schema_version=1,sequence=n,type=t,data=d)))
emit(1,'start',dict(task='task',session='session',profile='fixture'))
emit(2,'result',dict(status='completed',task='task',session='session',output='private result',checkpoint='a'*64))
"""
    if not success:
        code = "print('private malformed output')"
    monkeypatch.setattr(profile_owner, "_resolve", lambda *_: [sys.executable, "-c", code])
    write_config(tmp_path, agent=agent(), agent_profiles={"lscli-heartbeat": profile()})
    result = runtime.run_once(target_root=tmp_path)
    assert result["ok"] is success
    latest = json.loads((state_root(tmp_path)/"latest.json").read_text())
    log = json.loads((state_root(tmp_path)/latest["path"]/"command-log.json").read_text())
    entry = log["commands"][0]
    assert entry["stdout_tail"] == entry["stderr_tail"] == ""
    assert profile()["prompt"] not in json.dumps(log)
    if success:
        assert entry["protocol"]["completed"]
    else:
        assert entry["termination_reason"] == "protocol_error"


def test_owning_framework_resolution_rejects_ambient_module(tmp_path, monkeypatch):
    from types import ModuleType
    module = ModuleType("ls.untrusted_fixture")
    module.__file__ = str(tmp_path / "untrusted.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    with pytest.raises(ValueError, match="Ambient"):
        profile_owner._resolve(Path("/explicit/bin/lscli"), Path("/explicit/runtime"))


def test_framework_owner_is_called_without_provider_import(tmp_path, monkeypatch):
    from ls.core.agent import registration_dispatch
    before = set(sys.modules)
    monkeypatch.setattr(registration_dispatch, "resolve", lambda executable, root: ["protected"])
    assert profile_owner._resolve(Path("/explicit/bin/lscli"), tmp_path) == ["protected"]
    assert not any(n.startswith(("pydantic_ai", "openai")) for n in set(sys.modules)-before)


def test_leading_dash_profile_reaches_actual_parser(tmp_path, monkeypatch):
    import argparse
    from ls.core.agent.run_options import arguments
    monkeypatch.setattr(profile_owner, "_resolve", lambda *_: ["protected"])
    p = profile()
    p["profile"] = "-fixture"
    command = profile_owner.plan(p, agent(), "fixture", tmp_path)["command"]
    parser = argparse.ArgumentParser()
    arguments(parser)
    parsed = parser.parse_args(command[2:])
    assert parsed.profile == "-fixture" and parsed.workspace == tmp_path
