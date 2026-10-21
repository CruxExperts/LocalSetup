from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from ls.core.apply_packages import install_managed_packages
from ls.tests.test_install_flow import make_temp_repo


PACKAGE_NAME = "ls-omniroute-model-discovery"
PACKAGE_MODULES = (
    "__init__",
    "cli",
    "common",
    "probe",
    "observation",
    "observation_contract",
    "observation_rows",
)


def _assert_help_succeeds(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=cwd,
        env=env,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "usage:" in result.stdout
    assert "ModuleNotFoundError" not in combined


def test_installed_proxy_package_is_self_contained_and_directly_executable(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    source = Path(__file__).resolve().parents[2]
    (root / "ls" / "lib").mkdir()
    (root / "ls" / "lib" / "deps.py").write_bytes(
        (source / "ls" / "lib" / "deps.py").read_bytes()
    )

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
    helper = package / "scripts" / "omniroute_discover.py"
    module_root = package / "scripts" / "lib" / "omniroute_proxy"
    assert installed == [str(package)]
    assert helper.is_file()
    assert (package / "references" / "model-observation.schema.json").is_file()
    assert all((module_root / f"{name}.py").is_file() for name in PACKAGE_MODULES)
    assert stat.S_IMODE(helper.stat().st_mode) == 0o755

    outside_checkout = tmp_path / "outside-checkout"
    outside_checkout.mkdir()
    _assert_help_succeeds([sys.executable, "-I", str(helper), "--help"], cwd=outside_checkout)

    direct_env = {**os.environ, "PATH": str(Path(sys.executable).parent), "PYTHONPATH": ""}
    _assert_help_succeeds([str(helper), "--help"], cwd=outside_checkout, env=direct_env)
