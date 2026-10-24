"""Real managed sudo askpass with an isolated native private dialog viewer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import ShellCommandError, make_owner_binding, process_info, process_state
from ls.core.terminal_ops.sudo_dialog import SudoDialogContext
from ls.core.tmux_ops import managed


@pytest.mark.skipif(shutil.which("tmux") is None or not Path("/proc/self/stat").exists(),
                    reason="native Linux tmux is required")
@pytest.mark.parametrize("action", ["submit", "cancel", "delayed_submit"])
def test_managed_sudo_uses_private_native_dialog_once(monkeypatch, action: str) -> None:
    tmp_path = Path(tempfile.mkdtemp(prefix="ls-sd-"))
    root = Path(__file__).resolve().parents[2]
    backend = TmuxCommandBackend(config_file="/dev/null", socket_root=tmp_path / "family-sockets")
    if backend.capability().get("version") != "tmux 3.4":
        pytest.skip("private native dialog requires tmux 3.4")
    family_state = tmp_path / "family-state"
    service = ShellCommandService(family_state, backend=backend, source_root=root)
    owner = make_owner_binding(owner_pid=os.getpid(), agent_id="sudo-dialog-native-test")
    profile = resolve_profile(source_root=root, home=tmp_path / "home")
    outer = tmp_path / "viewer.sock"
    managed_endpoint = tmp_path / "managed.sock"
    managed_state = tmp_path / "managed-state"
    clean_env = {key: value for key, value in os.environ.items()
                 if key not in {"TMUX", "TMUX_PANE"} and not key.endswith("_SUDO_KEY")}
    clean_env["TERM"] = "xterm-256color"
    canary = "NativeManagedSudoCanary9"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    count = tmp_path / "sudo-count"
    workload_marker = tmp_path / "workload-executed"
    fake_sudo.write_text(
        f"#!{sys.executable}\n"
        "import hashlib,os,pathlib,subprocess,sys\n"
        "args=sys.argv[1:]\n"
        "assert args[:2]==['-A','--']\n"
        "pathlib.Path(os.environ['FAKE_SUDO_COUNT']).open('a').write('start\\n')\n"
        "reply=subprocess.run([os.environ['SUDO_ASKPASS'],'[sudo] password for test:'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,check=False)\n"
        "if reply.returncode or not reply.stdout: sys.exit(1)\n"
        "if hashlib.sha256(reply.stdout.rstrip(b'\\n')).hexdigest()!=os.environ['FAKE_SUDO_DIGEST']: sys.exit(2)\n"
        "os.execvpe(args[2],args[2:],os.environ)\n",
        encoding="utf-8",
    )
    fake_sudo.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GENERIC_SUDO_KEY", "")
    monkeypatch.setenv("FAKE_SUDO_COUNT", str(count))
    monkeypatch.setenv("FAKE_SUDO_DIGEST", hashlib.sha256(canary.encode()).hexdigest())
    clean_env.update(PATH=os.environ["PATH"], FAKE_SUDO_COUNT=str(count),
                     FAKE_SUDO_DIGEST=os.environ["FAKE_SUDO_DIGEST"])
    monkeypatch.setattr(managed, "_caller_identity", lambda pid, tty: f"fixture-controller:{pid}")

    def tmux(endpoint: Path, *args: str) -> str:
        completed = subprocess.run(["tmux", "-f", "/dev/null", "-S", str(endpoint), *args],
                                   env=clean_env, capture_output=True, text=True, timeout=5, check=True)
        return completed.stdout

    def eventually(predicate, timeout: float = 8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError("Native sudo dialog condition did not converge.")

    def call(method, *args, **kwargs):
        deadline = time.monotonic() + 7
        while True:
            try:
                return method(*args, **kwargs)
            except ShellCommandError as exc:
                if exc.code != "busy" or exc.effect != "none" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    handle = None
    runner = None
    viewers = []
    result: list[dict | BaseException] = []
    first = second = managed_target = None
    try:
        created = call(service.ensure, cwd=tmp_path, settings=profile["settings"],
                       settings_digest=profile["digest"], owner=owner,
                       profile_name=profile["name"], profile_base=profile["base_profile"],
                       layout_mode="canvas-v3")
        handle = created["handle"]
        attach = [sys.executable, str(root / "ls/tools/localsetup.py"), "--source-root", str(root),
                  "shell", "--state-root", str(family_state), "attach", "--session", created["session_alias"]]
        first = tmux(outer, "new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "120", "-y", "40",
                     "-s", "dialog", shlex.join(attach)).strip()
        tmux(outer, "set-option", "-g", "status", "off")
        eventually(lambda: "Workspace ready" in tmux(outer, "capture-pane", "-p", "-t", first))

        def primary_ready():
            directory, session, _ = service._load(handle)
            pins, unknown = service._pins(directory, remove_dead=False)
            primary = [pin for pin in pins if pin.get("primary_viewport") is True]
            return (not unknown and len(primary) == 1
                    and service._native_primary_client(directory, session, primary[0].get("viewer_pid")))

        eventually(primary_ready)
        second = tmux(outer, "new-window", "-P", "-F", "#{pane_id}", "-t", "dialog",
                      shlex.join(attach)).strip()
        eventually(lambda: "Workspace ready" in tmux(outer, "capture-pane", "-p", "-t", second))
        directory, _, _ = service._load(handle)
        pins, unknown = service._pins(directory, remove_dead=False)
        assert not unknown
        viewers = [process_info(pin["pid"]) for pin in pins]
        assert len(viewers) == 2 and all(item is not None and item["pid"] != os.getpid() for item in viewers)

        context = SudoDialogContext(handle, owner=owner, state_root=str(family_state))
        parent = tmux(managed_endpoint, "new-session", "-d", "-P", "-F", "#{pane_id}",
                      "-s", "managed", "-x", "80", "-y", "30", "-c", str(root), "/bin/bash", "-i").strip()
        monkeypatch.setenv("TMUX", f"{managed_endpoint},1,0")
        monkeypatch.setenv("TMUX_PANE", parent)
        picked = managed.pick(repository="localsetup", state_dir=str(managed_state))
        assert picked["state"] == "ready"
        managed_target = picked["target"]
        workload = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('executed')",
                    str(workload_marker)]

        def invoke() -> None:
            try:
                budget = 50 if action == "delayed_submit" else 12
                result.append(managed.run(managed_target, ["sudo", *workload], timeout=budget + 3,
                                          authentication_timeout=budget, state_dir=str(managed_state),
                                          dialog_context=context))
            except BaseException as exc:
                result.append(exc)

        runner = threading.Thread(target=invoke)
        runner.start()
        try:
            eventually(lambda: "Sudo authentication" in tmux(outer, "capture-pane", "-p", "-t", first), timeout=10)
        except AssertionError:
            observed = service._load(handle)[1]
            raise AssertionError({
                "run_result": [type(item).__name__ if isinstance(item, BaseException) else item.get("status")
                               for item in result],
                "run_errors": [getattr(item, "code", None) for item in result if isinstance(item, BaseException)],
                "sudo_receipt": [{key: item.get("sudo", {}).get(key)
                                  for key in ("status", "detail", "credential_lookup_status", "manual_authentication")}
                                 for item in result if isinstance(item, dict)],
                "sudo_invoked": count.exists(),
                "overlay": observed["native_layout"].get("overlay_lock"),
            }) from None
        assert "Sudo authentication" not in tmux(outer, "capture-pane", "-p", "-t", second)
        if action == "delayed_submit":
            # Exercise both former whole-exchange cutoffs (7s bridge, 35s askpass).
            # The owning session stays responsive while human input is pending.
            time.sleep(36)
            started = time.monotonic()
            assert call(service.status, handle, owner=owner)["state"] == "ready"
            assert time.monotonic() - started < 2
        if action in {"submit", "delayed_submit"}:
            tmux(outer, "send-keys", "-t", first, "-l", canary)
            assert canary not in tmux(outer, "capture-pane", "-p", "-t", first)
            tmux(outer, "send-keys", "-t", first, "C-s")
        else:
            tmux(outer, "send-keys", "-t", first, "Escape")
        runner.join(timeout=20)
        assert not runner.is_alive() and len(result) == 1 and isinstance(result[0], dict), result
        assert count.read_text(encoding="utf-8").splitlines() == ["start"]
        if action in {"submit", "delayed_submit"}:
            assert result[0].get("status") == "completed" and result[0].get("exit_code") == 0, result[0]
            assert workload_marker.read_text(encoding="utf-8") == "executed"
        else:
            assert not workload_marker.exists()
            assert result[0].get("status") != "running", result[0]
        assert context.current()
        for pane in (first, second):
            assert canary not in tmux(outer, "capture-pane", "-p", "-t", pane)
        assert canary not in tmux(managed_endpoint, "capture-pane", "-p", "-t", managed_target)
        for state_root in (family_state, managed_state):
            for path in state_root.rglob("*"):
                if stat.S_ISREG(path.lstat().st_mode):
                    assert canary.encode() not in path.read_bytes(), path
    finally:
        for endpoint in (outer, managed_endpoint):
            if endpoint.exists():
                tmux(endpoint, "kill-server")
                stopped = subprocess.run(["tmux", "-S", str(endpoint), "has-session"],
                                         env=clean_env, capture_output=True, timeout=5, check=False)
                assert stopped.returncode != 0, f"Owned tmux server remained at {endpoint}"
        if runner is not None:
            runner.join(timeout=5)
            assert not runner.is_alive(), "Managed sudo test worker did not stop"
        def viewer_exited(identity):
            current = process_info(identity["pid"])
            return (current is None or current["starttime"] != identity["starttime"]
                    or process_state(identity["pid"]) in {None, "Z", "X", "x"})
        eventually(lambda: all(viewer_exited(item) for item in viewers))
        if handle is not None:
            closed = call(service.close, handle, owner=owner)
            if closed.get("cleanup_state") != "removed":
                eventually(lambda: call(service.close, handle, owner=owner).get("cleanup_state") == "removed")
        shutil.rmtree(tmp_path)
        assert not tmp_path.exists()
