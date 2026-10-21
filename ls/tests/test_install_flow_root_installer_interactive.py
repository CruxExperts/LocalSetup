from __future__ import annotations

from ls.tests.test_install_flow import *

def test_root_installer_interactive_preserves_explicit_target_and_no_register_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    target = tmp_path / "target-repo"
    home = tmp_path / "home"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    target.mkdir()

    completed = run_installer_in_pty(
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
            "--no-register-shell",
        ],
        input_text="\n\n\n\n\nyes\n",
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert_scoped_adapter(target / ".agents" / "skills", "ls-localsetup-project-context")
    assert (target / ".localsetup/lock.json").is_file()
    assert not (root / ".cursor" / "skills").exists()
    assert not (root / ".agents" / "skills").exists()
    assert not (home / ".local" / "bin" / "localsetup").exists()


def test_root_installer_interactive_visual_flags_reach_wizard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    home = tmp_path / "home"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)

    completed = run_installer_in_pty(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--home",
            str(home),
            "--no-register-shell",
            "--no-color",
            "--glyphs",
            "ascii",
        ],
        input_text="\n\nq\n",
        cwd=tmp_path,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 130, combined
    assert "\033[1;" not in combined
    assert "\033[0m" not in combined
    assert "[SUGGESTED]" in combined
    assert "★" not in combined
    assert "Install canceled. No changes were applied." in combined


def test_root_installer_interactive_explicit_target_without_platforms_stays_global_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    target = tmp_path / "target-repo"
    home = tmp_path / "home"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)
    target.mkdir()

    completed = run_installer_in_pty(
        [
            str(root / "install"),
            "--directory",
            str(root),
            "--target-directory",
            str(target),
            "--home",
            str(home),
            "--no-register-shell",
        ],
        input_text="\n\n\n\nyes\n",
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
    assert (target / ".localsetup/lock.json").is_file()
    assert not (target / ".codex").exists()
    assert not (target / ".cursor").exists()


def test_root_installer_interactive_cancel_does_not_create_home_or_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    target = tmp_path / "target-repo"
    home = tmp_path / "home"
    shutil.copytree(source / "ls", root / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "install", root / "install")
    prepare_installer_source_metadata(source, root, monkeypatch)

    completed = run_installer_in_pty(
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
            "--no-register-shell",
        ],
        input_text="q\n",
        cwd=tmp_path,
    )

    assert completed.returncode == 130, completed.stderr + completed.stdout
    assert not home.exists()
    assert not target.exists()
