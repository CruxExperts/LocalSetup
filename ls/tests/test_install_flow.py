import json
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ls.core.apply as apply_mod
import ls.core.cli as cli_mod
import ls.core.conversion as conversion_mod
import ls.core.wizard as wizard
from ls.core.apply import apply_plan
from ls.core.boundary import scan_tar_for_leaks
from ls.core.cli import _split_csv
from ls.core.config import InstallConfig, load_install_config, merge_cli_config
from ls.core.context import build_agent_context, render_markdown_report
from ls.core.conversion import convert_repo
from ls.core.dependencies import ensure_dependencies
from ls.core.doctor import run_doctor
from ls.core.docs import generate_alias_outputs
from ls.core.hooks import run_maintainer_gate
from ls.core.lockfile import load_json
from ls.core.migration import conservative_migrate, detect_legacy_artifacts, scan_legacy_references
from ls.core.package import build_public_artifact, parse_sha256_file, verify_release_artifact
from ls.core.plan import build_install_plan
from ls.core.provenance import MARKER_JSON
from ls.core.rollback import rollback
from ls.core.repair import run_repair
from ls.core.schema import validate_json_schema
from ls.core.shell import detect_invocation_target, is_managed_shim, register_shell_command, shell_registration_status
from ls.core.skills import skill_taxonomy_payload
from ls.core.verify import verify_install
from ls.core.wizard import Choice, TerminalWizard, choose_many, choose_many_checkbox, choose_one, run_wizard
from ls.core.workflows import workflow_catalog_payload


class KeyboardInterruptStream(io.StringIO):
    def readline(self, *args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt


class FakeTtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeAsciiTtyStringIO(FakeTtyStringIO):
    @property
    def encoding(self) -> str:
        return "ascii"


class FakeKeyInput:
    def __init__(self, text: str) -> None:
        self.text = text
        self.offset = 0

    @property
    def remaining(self) -> bool:
        return self.offset < len(self.text)

    def read(self, size: int = 1) -> str:
        if not self.remaining:
            return ""
        value = self.text[self.offset : self.offset + size]
        self.offset += len(value)
        return value

    def fileno(self) -> int:
        return 0

    def isatty(self) -> bool:
        return True


def patch_fake_key_input(monkeypatch: pytest.MonkeyPatch, stream: FakeKeyInput) -> None:
    def fake_select(
        read_fds: list[int], write_fds: list[int], error_fds: list[int], timeout: float | None = None
    ) -> tuple[list[int], list[int], list[int]]:
        return (read_fds if stream.remaining else [], write_fds, error_fds)

    monkeypatch.setattr(wizard.select, "select", fake_select)


def enable_checkbox_key_mode(monkeypatch: pytest.MonkeyPatch, stream: FakeKeyInput) -> None:
    patch_fake_key_input(monkeypatch, stream)
    monkeypatch.setattr(wizard, "_can_use_checkbox_keys", lambda term: True)
    monkeypatch.setattr(wizard.termios, "tcgetattr", lambda fd: [])
    monkeypatch.setattr(wizard.termios, "tcsetattr", lambda fd, when, settings: None)
    monkeypatch.setattr(wizard.tty, "setcbreak", lambda fd: None)


def prepare_installer_source_metadata(source: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep copied source identity and child Python aligned with the test runtime."""
    for name in ("VERSION", "pyproject.toml"):
        shutil.copy2(source / name, root / name)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture
def installer_python_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give Bash bootstrap children the same dependencies as the test runner."""
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))


def make_temp_repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    (repo / "ls").mkdir(parents=True)
    shutil.copytree(source / "ls" / "config", repo / "ls" / "config")
    shutil.copytree(source / "ls" / "adapters", repo / "ls" / "adapters")
    shutil.copytree(source / "ls" / "core", repo / "ls" / "core")
    shutil.copytree(source / "ls" / "skills", repo / "ls" / "skills")
    shutil.copytree(source / "ls" / "workflows", repo / "ls" / "workflows")
    shutil.copytree(source / "ls" / "tools", repo / "ls" / "tools")
    shutil.copy2(source / "VERSION", repo / "VERSION")
    shutil.copy2(source / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy2(source / "uv.lock", repo / "uv.lock")
    shutil.copytree(source / "assets", repo / "assets")
    shutil.copytree(
        source / "ls" / "docs",
        repo / "ls" / "docs",
        ignore=shutil.ignore_patterns("local-context", "audits"),
    )
    (repo / ".github").mkdir()
    (repo / "README.md").write_text("# Localsetup\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    shutil.copy2(source / "REVIEW.md", repo / "REVIEW.md")
    return repo


def assert_scoped_adapter(path: Path, *packages: str) -> None:
    assert path.is_dir()
    assert not path.is_symlink()
    assert (path / ".localsetup-adapter.json").is_file()
    for package in packages:
        assert (path / package).is_symlink() or (path / package).is_dir()

def make_bootstrap_git_repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    shutil.copytree(source / "ls", repo / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "VERSION", repo / "VERSION")
    shutil.copy2(source / "pyproject.toml", repo / "pyproject.toml")
    (repo / "README.md").write_text("# Localsetup\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return repo


def make_bootstrap_git_repo_with_legacy_commit(tmp_path: Path) -> tuple[Path, str, str]:
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    marker = repo / "_localsetup" / "tools" / "localsetup.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("# legacy Localsetup entrypoint\n", encoding="utf-8")
    shutil.copy2(source / "VERSION", repo / "VERSION")
    shutil.copy2(source / "pyproject.toml", repo / "pyproject.toml")
    (repo / "README.md").write_text("# Localsetup\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "legacy"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    legacy_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    shutil.rmtree(repo / "_localsetup")
    shutil.copytree(source / "ls", repo / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    subprocess.run(["git", "add", "."], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "current"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return repo, legacy_commit, current_commit


def make_bootstrap_git_repo_with_release_tags(tmp_path: Path) -> tuple[Path, str, str]:
    repo = make_bootstrap_git_repo(tmp_path)
    subprocess.run(["git", "tag", "v4.8.7"], cwd=repo, text=True, capture_output=True, check=True)
    (repo / "README.md").write_text("# Localsetup\n\nrelease refresh\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "release"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "tag", "v4.8.9"], cwd=repo, text=True, capture_output=True, check=True)
    subprocess.run(["git", "tag", "v4.9.0-rc.1"], cwd=repo, text=True, capture_output=True, check=True)
    old_commit = subprocess.run(
        ["git", "rev-list", "-n", "1", "v4.8.7"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return repo, old_commit, current_commit


def run_installer_in_pty(
    command: list[str],
    *,
    input_text: str,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = shutil.which("script")
    if script is None:
        pytest.skip("script command is required for pseudo-terminal installer tests")
    log_path = cwd / "installer-pty.log"
    shell_command = " ".join(shlex.quote(part) for part in command)
    effective_env = {
        **os.environ,
        "LOCALSETUP_WIZARD_SELECTION_MODE": "line",
        "LOCALSETUP_WIZARD_DETAIL": "compact",
        **(env or {}),
    }
    return subprocess.run(
        [script, "-q", "-e", "-c", shell_command, str(log_path)],
        input=input_text,
        cwd=cwd,
        env=effective_env,
        text=True,
        capture_output=True,
        check=False,
    )

def run_localsetup_cli(root: Path, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(root / "ls" / "tools" / "localsetup.py"),
            "--source-root",
            str(root),
            "--home",
            str(home),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

__all__ = [name for name in globals() if not name.startswith("__")]
