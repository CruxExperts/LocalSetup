from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

from ls.core.apply_packages import install_managed_packages
from ls.tests.test_install_flow import make_temp_repo


PACKAGE_NAME = "ls-agent-capability-routing"


def test_installed_agent_routing_package_is_self_contained_and_static(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    package_root = home / ".local" / "share" / "localsetup" / "packages"
    installed = install_managed_packages(
        root,
        package_root,
        [PACKAGE_NAME],
        "skills",
        home=home,
        replace_func=os.replace,
    )

    package = package_root / PACKAGE_NAME
    script = package / "scripts" / "agent_routing.py"
    snapshot = package / "resources" / "model-capability-matrix" / "snapshot.json"
    assert installed == [str(package)]
    assert script.is_file() and snapshot.is_file()
    assert ".localsetup-maint" not in snapshot.read_text(encoding="utf-8")
    tree = ast.parse(script.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not imports.intersection({"socket", "subprocess", "ctypes", "requests", "urllib"})

    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    request = json.dumps(
        {
            "schema": "agent_routing_request_v1",
            "task_class": "routine",
            "risk": "low",
            "required_capabilities": [],
        }
    )
    result = subprocess.run(
        [sys.executable, "-I", str(script), "select", "--request", "-"],
        input=request,
        text=True,
        capture_output=True,
        cwd=outside,
        env={**os.environ, "PYTHONPATH": ""},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "selected"
    assert receipt["selection_policy"] == "static_reviewed_only"
