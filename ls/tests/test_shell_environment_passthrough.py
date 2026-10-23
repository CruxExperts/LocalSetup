"""The helper's Python bootstrap must not change a workload's import behavior."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import ShellCommandError, make_owner_binding


SOURCE_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("layout", ["legacy-v1"])
def test_native_workload_preserves_pythonpath_and_explicit_override(tmp_path, monkeypatch, layout):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux unavailable")
    with tempfile.TemporaryDirectory(prefix="ls-env-") as socket_directory:
        backend = TmuxCommandBackend(tmux, config_file="/dev/null", socket_root=Path(socket_directory))
        if backend.capability().get("state") != "available":
            pytest.skip("qualified Linux tmux backend unavailable")
        service = ShellCommandService(tmp_path / "state", backend=backend, source_root=SOURCE_ROOT)
        profile = resolve_profile("tmux", "agent-shell", source_root=SOURCE_ROOT, home=tmp_path / "home")
        owner = make_owner_binding(owner_pid=os.getpid())
        monkeypatch.delenv("PYTHONPATH", raising=False)
        shim_environment = {
            "LOCALSETUP_GLOBAL_SHIM": "1",
            "LOCALSETUP_SOURCE_ROOT": str(SOURCE_ROOT),
            "LOCALSETUP_HOME": str(tmp_path / "controller-home"),
        }
        for name, value in shim_environment.items():
            monkeypatch.setenv(name, value)
        session = service.ensure(cwd=tmp_path, settings=profile["settings"],
                                 settings_digest=profile["digest"], owner=owner, layout_mode=layout)
        def call_when_unlocked(method, *args, **kwargs):
            deadline = time.monotonic() + 3
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if (exc.code != "busy" or exc.effect != "none"
                            or time.monotonic() >= deadline):
                        raise
                    time.sleep(.02)

        probe = (
            "import json,os,sys; print(json.dumps({'pythonpath':os.environ.get('PYTHONPATH'),"
            "'site_packages':any('site-packages' in p for p in sys.path),"
            "'shim':{k:v for k,v in os.environ.items() if k in "
            "('LOCALSETUP_GLOBAL_SHIM','LOCALSETUP_SOURCE_ROOT','LOCALSETUP_HOME')}}))"
        )
        try:
            for index, (inherited, overrides, expected) in enumerate((
                (None, {}, None),
                ("", {}, ""),
                ("/example/user-imports", {}, "/example/user-imports"),
                ("/example/user-imports", {"PYTHONPATH": "/example/explicit-imports"},
                 "/example/explicit-imports"),
                (None, shim_environment, None),
            )):
                if inherited is None:
                    monkeypatch.delenv("PYTHONPATH", raising=False)
                else:
                    monkeypatch.setenv("PYTHONPATH", inherited)
                operation = call_when_unlocked(
                    service.run, session["handle"], f"environment-{index}",
                    argv=[sys.executable, "-S", "-c", probe],
                    env=overrides, cwd=tmp_path, owner=owner)
                result = service.wait(operation["operation_handle"], timeout=15, owner=owner)
                assert result["state"] == "completed"
                assert result["exit_code"] == 0
                assert json.loads(result["output"].strip()) == {
                    "pythonpath": expected, "site_packages": False,
                    "shim": {key: value for key, value in overrides.items() if key in shim_environment},
                }
            assert call_when_unlocked(service.close, session["handle"], owner=owner)["removed"] is True
        finally:
            # This directory contains only this test's private server sockets.
            for socket in Path(socket_directory).glob("s-*.sock"):
                subprocess.run([tmux, "-S", str(socket), "kill-server"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
