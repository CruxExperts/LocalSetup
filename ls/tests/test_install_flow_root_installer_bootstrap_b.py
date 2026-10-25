from __future__ import annotations

from ls.tests.test_install_flow import *

pytestmark = pytest.mark.usefixtures("installer_python_environment")

def test_root_installer_offline_release_lookup_without_managed_source_fails_actionably(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(tmp_path / "missing-remote"),
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "failed to discover the latest LocalSetup release and no managed source exists" in completed.stderr
    assert "LOCALSETUP_BOOTSTRAP_REF" in completed.stderr
    assert not managed_source.exists()


def test_root_installer_dirty_managed_source_fails_before_refresh(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, legacy_commit, _current_commit = make_bootstrap_git_repo_with_legacy_commit(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "--detach", legacy_commit], cwd=managed_source, text=True, capture_output=True, check=True)
    (managed_source / "local-edit.txt").write_text("do not overwrite\n", encoding="utf-8")
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "cannot refresh managed bootstrap source because it has uncommitted or untracked changes" in completed.stderr
    assert "--directory PATH" in completed.stderr
    assert (managed_source / "local-edit.txt").read_text(encoding="utf-8") == "do not overwrite\n"


def test_root_installer_non_git_managed_source_fails_actionably(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    (managed_source / "ls" / "tools").mkdir(parents=True)
    (managed_source / "ls" / "tools" / "localsetup.py").write_text("print('stale')\n", encoding="utf-8")
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "managed bootstrap source exists but is not a Git checkout" in completed.stderr
    assert "Move or remove it, or pass --directory PATH" in completed.stderr


def test_root_installer_unrelated_clean_git_managed_source_fails_without_mutation(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    managed_source.mkdir()
    (managed_source / "README.md").write_text("# Unrelated\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "unrelated"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    )
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode != 0
    assert "managed bootstrap source exists but is not a LocalSetup checkout" in completed.stderr
    assert before_head == after_head
    assert (managed_source / "README.md").read_text(encoding="utf-8") == "# Unrelated\n"
    assert not (managed_source / "ls").exists()


def test_root_installer_ignored_legacy_marker_in_unrelated_git_source_fails_without_mutation(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    managed_source.mkdir()
    (managed_source / ".gitignore").write_text("_localsetup/\n", encoding="utf-8")
    (managed_source / "_localsetup/tools").mkdir(parents=True)
    (managed_source / "_localsetup/tools/localsetup.py").write_text("# ignored unrelated marker\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "unrelated"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    )
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode != 0
    assert "managed bootstrap source exists but is not a LocalSetup checkout" in completed.stderr
    assert before_head == after_head
    assert (managed_source / "_localsetup/tools/localsetup.py").read_text(encoding="utf-8") == "# ignored unrelated marker\n"


def test_root_installer_tracked_legacy_marker_from_other_origin_fails_without_mutation(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    (managed_source / "_localsetup/tools").mkdir(parents=True)
    (managed_source / "_localsetup/tools/localsetup.py").write_text("# tracked unrelated marker\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "unrelated"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "unrelated-remote")],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    )
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=managed_source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode != 0
    assert "managed bootstrap source exists but is not a LocalSetup checkout" in completed.stderr
    assert before_head == after_head
    assert (managed_source / "_localsetup/tools/localsetup.py").read_text(encoding="utf-8") == "# tracked unrelated marker\n"


def test_root_installer_piped_bootstrap_selected_platform_attaches_caller_target(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    target = tmp_path / "target"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    target.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash", "-s", "--", "--non-interactive", "--yes", "--home", str(home), "--tools", "codex"],
            cwd=target,
            env=env,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr.decode()
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
    assert_scoped_adapter(target / ".agents" / "skills", "ls-localsetup-project-context")
    assert (target / ".localsetup/lock.json").is_file()
    assert not (managed_source / ".codex").exists()


def test_root_installer_explicit_bad_directory_does_not_bootstrap(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--directory", str(tmp_path / "missing"), "--non-interactive", "--yes"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "directory does not exist" in completed.stderr
    assert not managed_source.exists()


def test_root_installer_explicit_directory_ignores_managed_source_refresh(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2]
    install_path = source / "install"
    explicit_source = tmp_path / "explicit-source"
    outside = tmp_path / "outside"
    managed_source = tmp_path / "managed-source"
    home = tmp_path / "home"
    shutil.copytree(source / "ls", explicit_source / "ls", ignore=shutil.ignore_patterns("__pycache__", ".cache"))
    shutil.copy2(source / "VERSION", explicit_source / "VERSION")
    outside.mkdir()
    managed_source.mkdir()
    (managed_source / "not-git.txt").write_text("ignored\n", encoding="utf-8")
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(tmp_path / "missing-remote"),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [
            str(install_path),
            "--directory",
            str(explicit_source),
            "--home",
            str(home),
            "--non-interactive",
            "--yes",
            "--no-register-shell",
        ],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (managed_source / "not-git.txt").read_text(encoding="utf-8") == "ignored\n"
    assert not (managed_source / ".git").exists()
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()
