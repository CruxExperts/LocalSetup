"""Native headless tmux coverage for exact parent and managed child routing."""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

import pytest

from ls.core.tmux_ops import managed
from ls.core.terminal_ops import runtime
from ls.core.terminal_ops.backends import TmuxAdapter
from ls.core.terminal_ops.models import PaneError, PaneTransition


@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
def test_pick_reuses_exact_lower_child_and_runs_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed, "_caller_identity", lambda pid, tty: f"fixture-controller:{pid}")
    binary = shutil.which("tmux")
    assert binary is not None
    endpoint = tmp_path / "s.sock"
    source_root = Path(__file__).resolve().parents[2]
    state_dir = tmp_path / "state"

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([binary, "-S", str(endpoint), *args], capture_output=True,
                              text=True, timeout=10, check=False)

    started = tmux("-f", "/dev/null", "new-session", "-d", "-s", "terminal-test",
                   "-x", "80", "-y", "30", "-c", str(source_root), "/bin/bash", "-i")
    assert started.returncode == 0, started.stderr
    try:
        parent = tmux("list-panes", "-t", "terminal-test", "-F", "#{pane_id}")
        assert parent.returncode == 0
        parent_id = parent.stdout.strip()
        monkeypatch.setenv("TMUX", f"{endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent_id)
        created = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert created["state"] == "ready"
        assert created["changed"] is True
        assert created["parent_pane_id"] == parent_id
        child_id = created["target"]
        assert child_id != parent_id
        reused = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert reused["state"] == "ready"
        assert reused["changed"] is False
        assert reused["target"] == child_id
        command = managed.run(child_id, ["/usr/bin/printf", "native-child-success\\n"],
                              timeout=10, state_dir=str(state_dir))
        assert command["status"] == "completed"
        assert command["exit_code"] == 0
        captured = tmux("capture-pane", "-p", "-t", child_id)
        assert captured.returncode == 0
        assert "native-child-success" in captured.stdout
        _, service, request = managed._selected(child_id, str(state_dir))
        operation_id = uuid.uuid4().hex
        running_marker = tmp_path / "running-marker"
        started = service.transition(request, PaneTransition(operation_id, (
            sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(0.7)",
            str(running_marker))))
        assert started.state == "busy"
        deadline = time.monotonic() + 5
        while not running_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert running_marker.exists()
        observed_busy = service.inspect(request)
        assert observed_busy.state == "busy"
        assert observed_busy.binding is not None
        assert observed_busy.binding.pane_id == child_id
        while time.monotonic() < deadline:
            _, receipt = managed._read_operation(str(state_dir), request.binding_key, operation_id)
            if receipt.get("state") == "completed" and service.inspect(request).state == "ready":
                break
            time.sleep(0.02)
        assert receipt["state"] == "completed"
        assert service.inspect(request).state == "ready"
        panes = tmux("list-panes", "-t", "terminal-test", "-F", "#{pane_id}")
        assert panes.returncode == 0
        assert panes.stdout.splitlines() == [parent_id, child_id]
        second_parent = tmux("split-window", "-v", "-d", "-P", "-F", "#{pane_id}",
                             "-t", parent_id, "-c", str(source_root), "/bin/bash", "-i")
        assert second_parent.returncode == 0, second_parent.stderr
        monkeypatch.setenv("TMUX_PANE", second_parent.stdout.strip())
        other = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert other["state"] == "ready"
        assert other["target"] != child_id
        assert other["name"] == created["name"]
        monkeypatch.setenv("TMUX_PANE", parent_id)
        assert managed.pick(repository="localsetup", state_dir=str(state_dir))["target"] == child_id
        assert tmux("kill-pane", "-t", child_id).returncode == 0
        replacement = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert replacement["state"] == "ready" and replacement["changed"] is True
        assert replacement["target"] != child_id
        wrong_dir = tmp_path / "other-directory"
        wrong_dir.mkdir()
        assert tmux("send-keys", "-l", "-t", replacement["target"],
                    f"cd {shlex.quote(str(wrong_dir))}").returncode == 0
        assert tmux("send-keys", "-t", replacement["target"], "Enter").returncode == 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = tmux("display-message", "-p", "-t", replacement["target"],
                            "#{pane_current_path}").stdout.strip()
            if observed == str(wrong_dir):
                break
            time.sleep(0.02)
        assert observed == str(wrong_dir)
        while time.monotonic() < deadline:
            reused_after_cwd_change = managed.pick(repository="localsetup", state_dir=str(state_dir))
            if reused_after_cwd_change["state"] == "ready":
                break
            time.sleep(0.02)
        assert reused_after_cwd_change["state"] == "ready"
        assert reused_after_cwd_change["changed"] is False
        assert reused_after_cwd_change["target"] == replacement["target"]
        cwd_result = tmp_path / "restored-cwd"
        command = managed.run(replacement["target"], [sys.executable, "-c",
                                                        "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.getcwd())",
                                                        str(cwd_result)], timeout=10, state_dir=str(state_dir))
        assert command["status"] == "completed" and command["exit_code"] == 0
        assert cwd_result.read_text() == str(source_root)
        assert tmux("display-message", "-p", "-t", replacement["target"],
                    "#{pane_current_path}").stdout.strip() == str(source_root)
    finally:
        tmux("kill-server")


@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
def test_visible_sudo_uses_controller_secret_only_for_askpass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed, "_caller_identity", lambda pid, tty: f"fixture-controller:{pid}")
    binary = shutil.which("tmux")
    assert binary is not None
    endpoint = tmp_path / "s.sock"
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    mode = tmp_path / "sudo-mode"
    mode.write_text("nopasswd", encoding="utf-8")
    count = tmp_path / "sudo-count"
    env_leak = tmp_path / "credential-in-child"
    canary = "synthetic-controller-only-canary-4816"
    fake = fake_bin / "sudo"
    fake.write_text(
        f"#!{sys.executable}\n" +
        "import hashlib,os,pathlib,subprocess,sys\n"
        "args=sys.argv[1:]\n"
        "assert args[:2]==['-A','--']\n"
        "pathlib.Path(os.environ['FAKE_COUNT']).open('a').write('start\\n')\n"
        "if any(k.endswith('_SUDO_KEY') for k in os.environ): pathlib.Path(os.environ['FAKE_LEAK']).write_text('leaked')\n"
        "if pathlib.Path(os.environ['FAKE_MODE']).read_text()=='askpass':\n"
        "  response=subprocess.run([os.environ['SUDO_ASKPASS'],'[sudo] password for test:'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,check=False)\n"
        "  assert response.returncode==0\n"
        "  assert hashlib.sha256(response.stdout.rstrip(b'\\n')).hexdigest()==os.environ['FAKE_DIGEST']\n"
        "os.execvpe(args[2],args[2:],os.environ)\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    candidate_calls = []
    real_iter_candidates = runtime.iter_candidates
    def counted_candidates(*args: object, **kwargs: object):
        candidate_calls.append(1)
        yield from real_iter_candidates(*args, **kwargs)
    monkeypatch.setattr(runtime, "iter_candidates", counted_candidates)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_MODE", str(mode))
    monkeypatch.setenv("FAKE_COUNT", str(count))
    monkeypatch.setenv("FAKE_LEAK", str(env_leak))
    monkeypatch.setenv("FAKE_DIGEST", hashlib.sha256(canary.encode()).hexdigest())
    source_root = Path(__file__).resolve().parents[2]

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        clean_env = {key: value for key, value in os.environ.items() if not key.endswith("_SUDO_KEY")}
        return subprocess.run([binary, "-S", str(endpoint), *args], env=clean_env,
                              capture_output=True, text=True, timeout=10, check=False)

    started = tmux("-f", "/dev/null", "new-session", "-d", "-s", "sudo-test",
                   "-x", "80", "-y", "30", "-c", str(source_root / "ls"), "/bin/bash", "-i")
    assert started.returncode == 0, started.stderr
    try:
        parent = tmux("list-panes", "-t", "sudo-test", "-F", "#{pane_id}")
        assert parent.returncode == 0
        monkeypatch.setenv("TMUX", f"{endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent.stdout.strip())
        picked = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert picked["state"] == "ready"
        target = picked["target"]
        monkeypatch.setenv("LOCALSETUP_SUDO_KEY", canary)
        started_marker = tmp_path / "workload-started"
        stdin_result = tmp_path / "workload-stdin.json"
        workload = [sys.executable, "-c",
                    "import json,os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text('1'); "
                    "pathlib.Path(sys.argv[2]).write_text(json.dumps({'tty':os.isatty(0),"
                    "'line':sys.stdin.readline(),'cwd':os.getcwd()}))",
                    str(started_marker), str(stdin_result)]

        def send_workload_input() -> None:
            deadline = time.monotonic() + 8
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            if started_marker.exists():
                tmux("send-keys", "-l", "-t", target, "workload-stream")
                tmux("send-keys", "-t", target, "Enter")

        feeder = threading.Thread(target=send_workload_input)
        feeder.start()
        nopasswd = managed.run(target, ["sudo", *workload], timeout=10, state_dir=str(state_dir))
        feeder.join(timeout=2)
        if nopasswd.get("status") != "completed":
            print("RESULT:", nopasswd)
            print("PANE:", tmux("capture-pane", "-p", "-S", "-", "-t", target).stdout.replace(canary, "<redacted>"))
            print("MARKERS:", started_marker.exists(), count.read_text() if count.exists() else "missing")
            print("RECORDS:", [(path.name, json.loads(path.read_text()).get("state")) for path in state_dir.rglob("*.json")])
        assert nopasswd["status"] == "completed"
        assert nopasswd["exit_code"] == 0
        assert nopasswd["sudo"]["status"] == "completed"
        assert nopasswd["sudo"]["credential_candidates"] == [
            {"variable": "LOCALSETUP_SUDO_KEY", "status": "not_checked"},
            {"variable": "GENERIC_SUDO_KEY", "status": "not_checked"},
        ]
        assert candidate_calls == []
        assert json.loads(stdin_result.read_text()) == {
            "tty": True, "line": "workload-stream\n", "cwd": str(source_root / "ls"),
        }

        mode.write_text("askpass", encoding="utf-8")
        second_marker = tmp_path / "second-workload"
        askpass = managed.run(target, ["sudo", sys.executable, "-c",
                                        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('1')",
                                        str(second_marker)], timeout=10, state_dir=str(state_dir))
        assert askpass["status"] == "completed"
        assert candidate_calls == [1]
        assert askpass["exit_code"] == 0
        assert askpass["sudo"]["credential_candidates"][0] == {
            "variable": "LOCALSETUP_SUDO_KEY", "status": "provided_to_askpass",
        }
        assert second_marker.read_text() == "1"
        assert count.read_text().splitlines() == ["start", "start"]
        assert not env_leak.exists()
        assert canary not in tmux("capture-pane", "-p", "-S", "-", "-t", target).stdout
        for path in state_dir.rglob("*"):
            if path.is_file():
                assert canary.encode() not in path.read_bytes()
    finally:
        tmux("kill-server")


@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
def test_server_inherited_credential_is_empty_in_initial_managed_shell(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed, "_caller_identity", lambda pid, tty: f"fixture-controller:{pid}")
    binary = shutil.which("tmux")
    assert binary is not None
    endpoint = tmp_path / "s.sock"
    canary = "synthetic-server-only-credential-7182"
    root = Path(__file__).resolve().parents[2]

    def tmux(*args: str, server_secret: bool = False) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.endswith("_SUDO_KEY")}
        if server_secret:
            env["INHERITED_SUDO_KEY"] = canary
        return subprocess.run([binary, "-S", str(endpoint), *args], env=env,
                              capture_output=True, text=True, timeout=10, check=False)

    assert tmux("-f", "/dev/null", "new-session", "-d", "-s", "secret-test", "-x", "80", "-y", "30",
                "-c", str(root), "/bin/bash", "-i", server_secret=True).returncode == 0
    try:
        parent = tmux("list-panes", "-t", "secret-test", "-F", "#{pane_id}").stdout.strip()
        monkeypatch.setenv("TMUX", f"{endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent)
        monkeypatch.setenv("CONTROLLER_SUDO_KEY", "synthetic-controller-credential-8273")
        real_run = subprocess.run
        checked_helpers: set[str] = set()
        def checked_run(*args, **kwargs):
            command = args[0]
            if isinstance(command, list) and command and command[0] in {binary, "git", "readlink"}:
                environment = kwargs.get("env")
                assert environment is not None
                assert not any(name.endswith("_SUDO_KEY") for name in environment)
                checked_helpers.add(command[0])
            return real_run(*args, **kwargs)
        monkeypatch.setattr(subprocess, "run", checked_run)
        picked = managed.pick(repository="localsetup", state_dir=str(tmp_path / "state"))
        assert picked["state"] == "ready"
        assert {binary, "git", "readlink"} <= checked_helpers
        shell_pid = picked["binding"]["shell_pid"]
        initial = Path(f"/proc/{shell_pid}/environ").read_bytes()
        assert canary.encode() not in initial
        assert b"synthetic-controller-credential-8273" not in initial
        assert b"INHERITED_SUDO_KEY=" in initial
        assert b"INHERITED_SUDO_KEY=" + canary.encode() not in initial
    finally:
        tmux("kill-server")


@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
def test_caller_identity_requires_live_foreground_agent(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    binary = shutil.which("tmux")
    assert binary is not None
    endpoint = tmp_path / "s.sock"
    root = Path(__file__).resolve().parents[2]

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([binary, "-S", str(endpoint), *args], capture_output=True,
                              text=True, timeout=10, check=False)

    assert tmux("-f", "/dev/null", "new-session", "-d", "-s", "caller-test", "-x", "80", "-y", "30",
                "-c", str(root), "/bin/bash", "-i").returncode == 0
    try:
        parent = tmux("list-panes", "-t", "caller-test", "-F", "#{pane_id}").stdout.strip()
        monkeypatch.setenv("TMUX", f"{endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent)
        with pytest.raises(PaneError, match="foreground ancestry"):
            managed._tmux_context()

        def agent_identity(output: Path) -> str:
            child_code = "from ls.core.tmux_ops import managed; print(managed._tmux_context()['controller_id'])"
            driver = (
                "import json,pathlib,subprocess,sys; "
                f"result=subprocess.run([sys.executable,'-c',{child_code!r}],capture_output=True,text=True); "
                f"pathlib.Path({str(output)!r}).write_text(json.dumps({{'code':result.returncode,'out':result.stdout,'err':result.stderr}}))"
            )
            command = f"PYTHONPATH={shlex.quote(str(root))} {shlex.quote(sys.executable)} -c {shlex.quote(driver)}"
            assert tmux("send-keys", "-l", "-t", parent, command).returncode == 0
            assert tmux("send-keys", "-t", parent, "Enter").returncode == 0
            deadline = time.monotonic() + 8
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert output.exists(), tmux("capture-pane", "-p", "-t", parent).stdout
            result = json.loads(output.read_text())
            assert result["code"] == 0, result["err"]
            while time.monotonic() < deadline:
                current = tmux("display-message", "-p", "-t", parent, "#{pane_current_command}").stdout.strip()
                if current == "bash":
                    break
                time.sleep(0.02)
            return result["out"].strip()

        first = agent_identity(tmp_path / "first.json")
        second = agent_identity(tmp_path / "second.json")
        assert len(first) == 64 and len(second) == 64 and first != second
    finally:
        tmux("kill-server")


@pytest.mark.parametrize("failure", ["lost_split_response", "first_metadata_write"])
@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
def test_creation_reservation_blocks_duplicate_after_uncertain_native_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr(managed, "_caller_identity", lambda pid, tty: f"fixture-controller:{pid}")
    binary = shutil.which("tmux")
    assert binary is not None
    endpoint = tmp_path / "s.sock"
    root = Path(__file__).resolve().parents[2]
    state_dir = tmp_path / "state"

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([binary, "-S", str(endpoint), *args], capture_output=True,
                              text=True, timeout=10, check=False)

    assert tmux("-f", "/dev/null", "new-session", "-d", "-s", "reservation-test", "-x", "80",
                "-y", "30", "-c", str(root), "/bin/bash", "-i").returncode == 0
    try:
        parent = tmux("list-panes", "-t", "reservation-test", "-F", "#{pane_id}").stdout.strip()
        monkeypatch.setenv("TMUX", f"{endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent)
        real_run = TmuxAdapter._run
        split_count = 0
        metadata_failed = False

        def uncertain_run(self, request, args, **kwargs):
            nonlocal split_count, metadata_failed
            if args[0] == "split-window":
                split_count += 1
                result = real_run(self, request, args, **kwargs)
                if failure == "lost_split_response":
                    raise PaneError("split_unknown", "Synthetic lost response after native creation.", effect="unknown")
                return result
            if args[0] == "set-option" and failure == "first_metadata_write" and not metadata_failed:
                metadata_failed = True
                return subprocess.CompletedProcess(args, 1, "", "synthetic metadata failure")
            return real_run(self, request, args, **kwargs)

        monkeypatch.setattr(TmuxAdapter, "_run", uncertain_run)
        with pytest.raises(PaneError):
            managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert split_count == 1
        records = list((state_dir / "terminal_ops" / "bindings").glob("*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_text())["creation"]["state"] == "reserved"
        second = managed.pick(repository="localsetup", state_dir=str(state_dir))
        assert second["state"] == "unknown" and second["changed"] is False
        assert split_count == 1
        assert len(tmux("list-panes", "-t", "reservation-test", "-F", "#{pane_id}").stdout.splitlines()) == 2
    finally:
        tmux("kill-server")
