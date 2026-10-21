from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls" / "skills" / "ls-omniroute-model-discovery" / "scripts" / "omniroute_discover.py"


def load_probe():
    spec = importlib.util.spec_from_file_location("omniroute_discover_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_preflight_reports_missing_key_and_registration_commands(monkeypatch) -> None:
    probe = load_probe()
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setattr(
        probe,
        "fetch_json",
        lambda session, url, api_key, timeout: {
            "ok": False,
            "status": 401,
            "error": "HTTP 401: Unauthorized",
            "hint": "check auth",
        },
    )

    report = probe.access_preflight(
        "http://localhost:20128",
        "OMNIROUTE_API_KEY",
        None,
        "runtime",
        1.0,
        True,
    )

    assert report["access_ok"] is False
    assert report["env"]["api_key_env_set"] is False
    commands = "\n".join(report["registration_commands"])
    assert "PASTE_OMNIROUTE_API_KEY_HERE" in commands
    assert "OMNIROUTE_BASE_URL=http://localhost:20128" in commands
    assert "Relaunch terminals" in commands
    assert "sk-" not in commands
    assert re.search(r"(^|[^>])> ~/.config/environment\.d/omniroute\.conf", commands) is None
    assert ">> ~/.config/environment.d/omniroute.conf" in commands


def test_preflight_marks_admin_compatible_without_printing_key(monkeypatch) -> None:
    probe = load_probe()
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-secret")
    monkeypatch.setattr(
        probe,
        "fetch_json",
        lambda session, url, api_key, timeout: {
            "ok": True,
            "status": 200,
            "summary": {"type": "object"},
        },
    )

    api_key = probe.load_api_key("OMNIROUTE_API_KEY")
    report = probe.access_preflight(
        "http://localhost:20128",
        "OMNIROUTE_API_KEY",
        api_key,
        "admin",
        1.0,
        False,
    )

    assert report["access_ok"] is True
    assert report["env"]["api_key_value_redacted"] == "***REDACTED***"
    assert "sk-secret" not in probe.render_preflight_markdown(report)


def test_probe_redacts_secret_like_invalid_json_and_error_samples(monkeypatch) -> None:
    probe = load_probe()

    class DummyResponse:
        status_code = 500
        ok = False
        reason = "Authorization: Bearer sk-reason token=reason-token"
        headers = {"Content-Type": "text/plain"}

        def iter_content(self, chunk_size):
            yield b"api_key=raw-key token=raw-token sk-body"

        def close(self):
            return None

    class DummySession:
        def get(self, *args, **kwargs):
            return DummyResponse()

    result = probe.fetch_json(
        DummySession(),
        "http://localhost:20128/api/monitoring/health",
        "sk-env",
        1.0,
    )

    rendered = repr(result)
    assert "sk-reason" not in rendered
    assert "reason-token" not in rendered
    assert "raw-key" not in rendered
    assert "raw-token" not in rendered
    assert "sk-body" not in rendered
    assert "***REDACTED***" in rendered
