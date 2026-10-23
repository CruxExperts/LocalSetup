"""The helper's Python bootstrap must not change a workload's import behavior."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import make_owner_binding


SOURCE_ROOT = Path(__file__).resolve().parents[2]


def test_native_workload_preserves_pythonpath_and_explicit_override(tmp_path, monkeypatch):
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
        session = service.ensure(cwd=tmp_path, settings=profile["settings"],
                                 settings_digest=profile["digest"], owner=owner)
        probe = (
            "import json,os,sys; print(json.dumps({'pythonpath':os.environ.get('PYTHONPATH'),"
            "'site_packages':any('site-packages' in p for p in sys.path)}))"
        )
        try:
            for index, (inherited, overrides, expected) in enumerate((
                (None, {}, None),
                ("", {}, ""),
                ("/example/user-imports", {}, "/example/user-imports"),
                ("/example/user-imports", {"PYTHONPATH": "/example/explicit-imports"},
                 "/example/explicit-imports"),
            )):
                if inherited is None:
                    monkeypatch.delenv("PYTHONPATH", raising=False)
                else:
                    monkeypatch.setenv("PYTHONPATH", inherited)
                operation = service.run(session["handle"], f"environment-{index}",
                                        argv=[sys.executable, "-S", "-c", probe],
                                        env=overrides, cwd=tmp_path, owner=owner)
                result = service.wait(operation["operation_handle"], timeout=15, owner=owner)
                assert result["state"] == "completed"
                assert result["exit_code"] == 0
                assert json.loads(result["output"].strip()) == {
                    "pythonpath": expected, "site_packages": False,
                }
            assert service.close(session["handle"], owner=owner)["removed"] is True
        finally:
            # This directory contains only this test's private server sockets.
            for socket in Path(socket_directory).glob("s-*.sock"):
                subprocess.run([tmux, "-S", str(socket), "kill-server"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
