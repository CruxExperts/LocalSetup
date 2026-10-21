import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls" / "skills" / "ls-omniroute-task-triage" / "scripts" / "omniroute_api.py"


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_env_commands_use_placeholders_not_secret_values() -> None:
    result = run_cli(
        "--base-url",
        "http://localhost:20128",
        "--api-key-env",
        "OMNIROUTE_API_KEY",
        "env-commands",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    commands = "\n".join(payload["commands"])
    assert "PASTE_OMNIROUTE_API_KEY_HERE" in commands
    assert "OMNIROUTE_API_KEY" in commands
    assert "Relaunch terminals" in commands
    assert re.search(r"(^|[^>])> ~/.config/environment\.d/omniroute\.conf", commands) is None
    assert ">> ~/.config/environment.d/omniroute.conf" in commands


def test_base_url_rejects_embedded_credentials_without_leaking_secret() -> None:
    result = run_cli(
        "--base-url",
        "http://user:secret@localhost:20128",
        "preflight",
    )

    assert result.returncode == 2
    assert "base-url must not include credentials" in result.stderr
    assert "secret" not in result.stdout
    assert "secret" not in result.stderr


def test_mutating_request_requires_explicit_flag() -> None:
    result = run_cli(
        "request",
        "POST",
        "/api/settings",
        "--body-json",
        '{"example": true}',
    )

    assert result.returncode == 2
    assert "POST requires --allow-mutation" in result.stderr


def test_request_runs_required_access_preflight_before_target_call() -> None:
    requested_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            requested_paths.append(self.path)
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"forbidden"}')

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            requested_paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"unexpected":true}')

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["OMNIROUTE_API_KEY"] = "sk-test"
        result = run_cli(
            "--base-url",
            f"http://127.0.0.1:{server.server_port}",
            "request",
            "POST",
            "/api/settings/target",
            "--required-access",
            "admin",
            "--allow-mutation",
            "--body-json",
            '{"example": true}',
            env=env,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "target request was not sent" in payload["error"]
    assert "/api/settings/target" not in requested_paths


def test_request_redacts_secret_like_response_strings() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"error":"Authorization: Bearer sk-json api_key=raw token=plain"}'
            )

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["OMNIROUTE_API_KEY"] = "sk-env"
        result = run_cli(
            "--base-url",
            f"http://127.0.0.1:{server.server_port}",
            "request",
            "GET",
            "/v1/models",
            "--required-access",
            "runtime",
            env=env,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert "sk-json" not in result.stdout
    assert "api_key=raw" not in result.stdout
    assert "token=plain" not in result.stdout
    assert "***REDACTED***" in result.stdout
