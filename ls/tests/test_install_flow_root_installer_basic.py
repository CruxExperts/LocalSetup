from __future__ import annotations

from ls.tests.test_install_flow import *

def test_root_installer_forwards_custom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "repo"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    home = tmp_path / "custom-home"

    completed = subprocess.run(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--home",
            str(home),
            "--tools",
            "codex",
            "--non-interactive",
            "--yes",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
    assert_scoped_adapter(root / ".agents" / "skills", "ls-localsetup-project-context")
    assert (home / ".local" / "bin" / "localsetup").is_file()


def test_root_installer_supports_target_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    target = tmp_path / "target-repo"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    target.mkdir()
    home = tmp_path / "custom-home"

    completed = subprocess.run(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--target-directory",
            str(target),
            "--home",
            str(home),
            "--tools",
            "cursor",
            "--non-interactive",
            "--yes",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
    assert_scoped_adapter(target / ".agents" / "skills", "ls-localsetup-project-context")
    assert (target / ".localsetup/lock.json").is_file()
    assert (home / ".local" / "bin" / "localsetup").is_file()
    assert not (root / ".cursor" / "skills").exists()
    assert not (root / ".agents" / "skills").exists()


def test_root_installer_target_directory_without_platforms_uses_auto_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    target = tmp_path / "target-repo"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    target.mkdir()
    home = tmp_path / "custom-home"

    completed = subprocess.run(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--target-directory",
            str(target),
            "--home",
            str(home),
            "--non-interactive",
            "--yes",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert payload["auto_mode"] == "default_new_repo"
    assert (target / ".localsetup/lock.json").is_file()
    assert not (target / ".agents" / "skills").exists()
    assert "without --tools/--platforms" not in completed.stderr


def test_root_installer_non_interactive_no_register_shell_skips_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "repo"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    home = tmp_path / "custom-home"

    completed = subprocess.run(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--home",
            str(home),
            "--non-interactive",
            "--yes",
            "--no-register-shell",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
    assert not (home / ".local" / "bin" / "localsetup").exists()


def test_root_installer_non_interactive_visual_flags_keep_json_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "repo"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    home = tmp_path / "custom-home"

    completed = subprocess.run(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--home",
            str(home),
            "--non-interactive",
            "--yes",
            "--no-register-shell",
            "--color",
            "always",
            "--glyphs",
            "unicode",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["attachment"]["platforms"] == []
    assert "\033[" not in completed.stdout
    assert "[OK]" not in completed.stdout


@pytest.mark.parametrize("help_alias", ["--help", "-h", "-?", "--?"])
def test_root_installer_help_mentions_target_directory_and_global_only_defaults(help_alias) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"

    completed = subprocess.run(
        [str(install_path), help_alias],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "--target-directory PATH" in completed.stdout
    assert "LocalSetup auto mode" in completed.stdout
    assert "Explicit values override auto mode" in completed.stdout
    assert "--non-interactive" in completed.stdout
    assert "Automation mode" in completed.stdout
    assert "--no-register-shell" in completed.stdout
    assert "--color MODE" in completed.stdout
    assert "--no-color" in completed.stdout
    assert "--glyphs MODE" in completed.stdout


def test_root_installer_stdin_without_tty_requires_interactive_or_automation(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    outside = tmp_path / "outside"
    outside.mkdir()

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash"],
            cwd=outside,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    stderr = completed.stderr.decode()
    assert completed.returncode != 0
    assert stderr.strip() == "Error: interactive installer requires a terminal. Run from a TTY, or use --non-interactive --yes for automation."
    assert "BASH_SOURCE" not in stderr
    assert "unbound variable" not in stderr


def test_root_installer_non_interactive_requires_yes_without_bash_source_warning(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    outside = tmp_path / "outside"
    outside.mkdir()

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash", "-s", "--", "--non-interactive"],
            cwd=outside,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    stderr = completed.stderr.decode()
    assert completed.returncode != 0
    assert stderr.strip() == "Error: automation mode requires --non-interactive --yes"
    assert "BASH_SOURCE" not in stderr
    assert "unbound variable" not in stderr


def test_root_installer_stdin_help_without_bash_source_warning(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    outside = tmp_path / "outside"
    outside.mkdir()

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash", "-s", "--", "--help"],
            cwd=outside,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    stderr = completed.stderr.decode()
    stdout = completed.stdout.decode()
    assert completed.returncode == 0
    assert "curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --" in stdout
    assert "curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s -- --non-interactive --yes" in stdout
    assert "--target-directory PATH" in stdout
    assert "BASH_SOURCE" not in stderr
    assert "unbound variable" not in stderr


def test_root_installer_sync_env_rejects_old_uv_before_sync(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    fake_uv = tmp_path / "uv"
    sync_marker = tmp_path / "sync-called"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'uv 0.4.26'; exit 0; fi\n"
        f"if [[ \"$*\" == *sync* ]]; then touch {shlex.quote(str(sync_marker))}; fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    completed = subprocess.run(
        [
            str(install_path),
            "--directory",
            str(Path(__file__).resolve().parents[2]),
            "--tools",
            "codex",
            "--sync-env",
            "--non-interactive",
            "--yes",
            "--home",
            str(tmp_path / "home"),
            "--target-directory",
            str(tmp_path / "target"),
        ],
        env={**os.environ, "LOCALSETUP_UV_BIN": str(fake_uv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "uv 0.4.26 is too old; LocalSetup requires uv >= 0.4.27" in completed.stderr
    assert not sync_marker.exists()
