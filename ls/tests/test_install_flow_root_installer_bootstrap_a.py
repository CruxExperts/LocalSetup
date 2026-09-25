from __future__ import annotations

from ls.tests.test_install_flow import *

def test_root_installer_piped_bootstrap_global_only_uses_managed_source(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    source = Path(__file__).resolve().parents[2]
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(source / name, bootstrap_repo / name)
    subprocess.run(["git", "add", "pyproject.toml", "uv.lock"], cwd=bootstrap_repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "add locked runtime"],
        cwd=bootstrap_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    fake_uv = tmp_path / "uv"
    sync_marker = tmp_path / "sync-called"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'uv 0.11.21'; exit 0; fi\n"
        f"if [[ \" $* \" == *' sync --locked --no-dev '* ]]; then echo 'uv sync progress'; touch {shlex.quote(str(sync_marker))}; exit 0; fi\n"
        "if [[ \" $* \" == *' lock '* ]]; then exit 0; fi\n"
        f"[[ -f {shlex.quote(str(sync_marker))} ]] || exit 97\n"
        "while [[ \"$1\" != \"run\" ]]; do shift; done\n"
        "shift\n"
        "while [[ \"$1\" == --* ]]; do shift; done\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
        "LOCALSETUP_UV_BIN": str(fake_uv),
    }

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash", "-s", "--", "--non-interactive", "--yes", "--home", str(home)],
            cwd=outside,
            env=env,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr.decode()
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert b"uv sync progress" not in completed.stdout
    assert b"uv sync progress" in completed.stderr
    assert (managed_source / "ls/tools/localsetup.py").is_file()
    assert sync_marker.is_file()
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    assert (home / ".local/bin/localsetup").is_file()
    assert not (outside / ".codex").exists()
    assert not (outside / ".localsetup/lock.json").exists()
    ignored = subprocess.run(
        ["git", "check-ignore", ".venv/pyvenv.cfg"],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert ignored.returncode == 0, ignored.stderr

def test_root_installer_retry_sync_keeps_automation_stdout_json_only(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    source = Path(__file__).resolve().parents[2]
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(source / name, bootstrap_repo / name)
    subprocess.run(["git", "add", "pyproject.toml", "uv.lock"], cwd=bootstrap_repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "add locked runtime"],
        cwd=bootstrap_repo,
        text=True,
        capture_output=True,
        check=True,
    )
    managed_source = tmp_path / "managed-source"
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    source_venv = managed_source / ".venv"
    source_python = source_venv / "bin" / "python"
    source_python.parent.mkdir(parents=True)
    source_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    source_python.chmod(0o755)
    (source_venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    fake_uv = tmp_path / "uv"
    sync_attempt = tmp_path / "sync-attempt"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'uv 0.11.21'; exit 0; fi\n"
        f"if [[ \" $* \" == *' sync --locked --no-dev '* ]]; then if [[ ! -f {shlex.quote(str(sync_attempt))} ]]; then touch {shlex.quote(str(sync_attempt))}; echo 'virtual environment failed'; exit 1; fi; echo 'uv sync retry progress'; exit 0; fi\n"
        "if [[ \" $* \" == *' lock '* ]]; then exit 0; fi\n"
        "while [[ \"$1\" != \"run\" ]]; do shift; done\n"
        "shift\n"
        "while [[ \"$1\" == --* ]]; do shift; done\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    completed = subprocess.run(
        [
            "bash",
            str(install_path),
            "--directory",
            str(managed_source),
            "--sync-env",
            "--non-interactive",
            "--yes",
            "--home",
            str(home),
        ],
        cwd=outside,
        env={**os.environ, "LOCALSETUP_UV_BIN": str(fake_uv)},
        text=False,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert b"uv sync retry progress" not in completed.stdout
    assert b"uv sync retry progress" in completed.stderr
    assert sync_attempt.is_file()

def test_root_installer_piped_bootstrap_requires_explicit_uv_install_when_missing(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    source = Path(__file__).resolve().parents[2]
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(source / name, bootstrap_repo / name)
    subprocess.run(["git", "add", "pyproject.toml", "uv.lock"], cwd=bootstrap_repo, text=True, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=LocalSetup Test", "-c", "user.email=test@example.invalid", "commit", "-m", "add locked runtime"],
        cwd=bootstrap_repo,
        text=True,
        capture_output=True,
        check=True,
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_marker = tmp_path / "curl-called"
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(f"#!/usr/bin/env bash\ntouch {shlex.quote(str(curl_marker))}\n", encoding="utf-8")
    fake_curl.chmod(0o755)

    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(tmp_path / "managed-source"),
    }

    with install_path.open("rb") as stdin:
        completed = subprocess.run(
            ["bash", "-s", "--", "--non-interactive", "--yes", "--home", str(home)],
            cwd=tmp_path,
            env=env,
            stdin=stdin,
            text=False,
            capture_output=True,
            check=False,
        )

    assert completed.returncode != 0
    assert b"pass --install-uv" in completed.stderr
    assert not curl_marker.exists()


def test_root_installer_explicit_source_keeps_sync_opt_in(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    source = Path(__file__).resolve().parents[2]
    fake_uv = tmp_path / "uv"
    sync_marker = tmp_path / "sync-called"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \" $* \" == *' sync '* ]]; then touch {shlex.quote(str(sync_marker))}; exit 97; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    completed = subprocess.run(
        [
            str(install_path),
            "--directory",
            str(source),
            "--target-directory",
            str(tmp_path / "target"),
            "--home",
            str(tmp_path / "home"),
            "--no-register-shell",
            "--non-interactive",
            "--yes",
        ],
        env={**os.environ, "LOCALSETUP_UV_BIN": str(fake_uv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not sync_marker.exists()


def test_root_installer_refreshes_clean_stale_managed_source_before_wizard(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, legacy_commit, current_commit = make_bootstrap_git_repo_with_legacy_commit(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "--detach", legacy_commit], cwd=managed_source, text=True, capture_output=True, check=True)
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = run_installer_in_pty(
        [str(install_path), "--home", str(home), "--no-register-shell"],
        input_text="\n\nq\n",
        cwd=outside,
        env=env,
    )

    combined = completed.stdout + completed.stderr
    refreshed = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 130, combined
    assert refreshed == current_commit
    assert "invalid choice: 'wizard'" not in combined


def test_root_installer_migrates_clean_legacy_managed_source_with_rollback_evidence(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, legacy_commit, current_commit = make_bootstrap_git_repo_with_legacy_commit(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "state"
    target = make_temp_repo(tmp_path / "target")
    outside.mkdir()
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "main",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source / ".git" / ".."),
    }

    initial = subprocess.run(
        [
            str(install_path),
            "--non-interactive",
            "--yes",
            "--home",
            str(home),
            "--target-directory",
            str(target),
            "--platforms",
            "codex",
            "--no-register-shell",
        ],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert initial.returncode == 0, initial.stderr
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    assert_scoped_adapter(target / ".agents/skills", "ls-context")

    subprocess.run(["git", "clean", "-fdx"], cwd=managed_source, text=True, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "--detach", legacy_commit], cwd=managed_source, text=True, capture_output=True, check=True)
    completed = subprocess.run(
        [
            str(install_path),
            "--non-interactive",
            "--yes",
            "--home",
            str(home),
            "--target-directory",
            str(target),
            "--platforms",
            "codex",
            "--no-register-shell",
        ],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    refreshed = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 0, completed.stderr
    assert refreshed == current_commit
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
    assert_scoped_adapter(target / ".agents/skills", "ls-context")
    rollback_root = home / ".local/share/localsetup/state/source-migrations"
    manifests = sorted(rollback_root.glob("legacy-source-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["source_path"] == str(managed_source)
    assert manifest["original_head"] == legacy_commit
    assert manifest["target_ref"] == "main"
    bundle_path = Path(manifest["bundle_path"])
    assert bundle_path.is_file()
    bundle_verified = subprocess.run(
        ["git", "-C", str(managed_source), "bundle", "verify", str(bundle_path)],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "is okay" in bundle_verified.stderr
    rollback_clone = tmp_path / "rollback-clone"
    subprocess.run(["git", "clone", str(bundle_path), str(rollback_clone)], text=True, capture_output=True, check=True)
    bundled_legacy = subprocess.run(
        ["git", "-C", str(rollback_clone), "cat-file", "-e", f"{legacy_commit}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert bundled_legacy.returncode == 0, bundled_legacy.stderr

    cli_base = [
        sys.executable,
        str(managed_source / "ls/tools/localsetup.py"),
        "--home",
        str(home),
        "--source-root",
        str(managed_source),
        "--target-directory",
        str(target),
    ]
    for command in (["--version"], ["doctor"], ["verify", "--platforms", "codex"]):
        checked = subprocess.run([*cli_base, *command], text=True, capture_output=True, check=False)
        assert checked.returncode == 0, checked.stderr


def test_root_installer_discovers_latest_stable_release_tag_for_managed_source(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, old_commit, current_commit = make_bootstrap_git_repo_with_release_tags(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "--detach", old_commit], cwd=managed_source, text=True, capture_output=True, check=True)
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes", "--home", str(home), "--no-register-shell"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    refreshed = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 0, completed.stderr
    assert refreshed == current_commit
    assert (managed_source / "README.md").read_text(encoding="utf-8") == "# Localsetup\n\nrelease refresh\n"
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()


def test_root_installer_refuses_numeric_tag_fallback_across_major_lines(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, _old_commit, current_commit = make_bootstrap_git_repo_with_release_tags(tmp_path / "bootstrap")
    subprocess.run(["git", "tag", "v5.6.2", current_commit], cwd=bootstrap_repo, text=True, capture_output=True, check=True)
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes", "--home", str(home), "--no-register-shell"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "failed to discover the latest LocalSetup release" in completed.stderr
    assert "set LOCALSETUP_BOOTSTRAP_REF" in completed.stderr
    assert not managed_source.exists()


def test_root_installer_filters_github_release_api_payload_before_tag_fallback(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, _old_commit, current_commit = make_bootstrap_git_repo_with_release_tags(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    releases = tmp_path / "releases.json"
    outside.mkdir()
    releases.write_text(
        json.dumps(
            [
                {
                    "tag_name": "v4.9.0-rc.1",
                    "draft": False,
                    "prerelease": True,
                    "published_at": "2026-05-20T00:00:00Z",
                },
                {
                    "tag_name": "v4.8.10",
                    "draft": True,
                    "prerelease": False,
                    "published_at": "2026-05-19T03:00:00Z",
                },
                {
                    "tag_name": "v4.8.9",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-05-19T02:00:00Z",
                },
                {
                    "tag_name": "v4.8.7",
                    "draft": False,
                    "prerelease": False,
                    "published_at": "2026-05-18T02:00:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "",
        "LOCALSETUP_BOOTSTRAP_RELEASES_URL": releases.resolve().as_uri(),
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes", "--home", str(home), "--no-register-shell"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    checked_out = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 0, completed.stderr
    assert checked_out == current_commit


def test_root_installer_bootstrap_ref_override_skips_latest_release_discovery(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo, old_commit, _current_commit = make_bootstrap_git_repo_with_release_tags(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(bootstrap_repo),
        "LOCALSETUP_BOOTSTRAP_REF": "v4.8.7",
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes", "--home", str(home), "--no-register-shell"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    checked_out = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 0, completed.stderr
    assert checked_out == old_commit
    assert (managed_source / "README.md").read_text(encoding="utf-8") == "# Localsetup\n"


def test_root_installer_offline_release_lookup_reuses_existing_managed_source(tmp_path: Path) -> None:
    install_path = Path(__file__).resolve().parents[2] / "install"
    bootstrap_repo = make_bootstrap_git_repo(tmp_path / "bootstrap")
    outside = tmp_path / "outside"
    home = tmp_path / "home"
    managed_source = tmp_path / "managed-source"
    outside.mkdir()
    subprocess.run(["git", "clone", str(bootstrap_repo), str(managed_source)], text=True, capture_output=True, check=True)
    before = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    env = {
        **os.environ,
        "LOCALSETUP_BOOTSTRAP_REPO": str(tmp_path / "missing-remote"),
        "LOCALSETUP_BOOTSTRAP_SOURCE_DIR": str(managed_source),
    }

    completed = subprocess.run(
        [str(install_path), "--non-interactive", "--yes", "--home", str(home), "--no-register-shell"],
        cwd=outside,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    after = subprocess.run(
        ["git", "-C", str(managed_source), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert completed.returncode == 0, completed.stderr
    assert "failed to discover the latest LocalSetup release; using existing managed source" in completed.stderr
    assert after == before
    assert (home / ".local/share/localsetup/packages/ls-context").is_dir()
