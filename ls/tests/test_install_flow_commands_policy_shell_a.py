from __future__ import annotations

from ls.tests.test_install_flow import *

def test_phase3_command_family_outputs_json(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "consumer"
    target.mkdir()
    (target / "package.json").write_text('{"scripts":{"test":"node --test"}}\n', encoding="utf-8")
    tool = root / "ls" / "tools" / "localsetup.py"
    commands = [
        ["skill", "search", "context"],
        ["skill", "info", "ls-context"],
        ["workflow", "search", "audit"],
        ["skill", "info", "ls-framework-audit"],
        ["why", "--packs", "core"],
        ["graph"],
        ["audit-global-first"],
        ["adopt", "--target-directory", str(target)],
        ["diff", "--tools", "codex"],
    ]
    for args in commands:
        completed = subprocess.run(
            [sys.executable, str(tool), "--repo", str(root), "--home", str(home), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, args + [completed.stderr, completed.stdout]
        payload = json.loads(completed.stdout)
        assert isinstance(payload, dict)


def test_global_first_audit_reports_target_legacy_surfaces(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    target = tmp_path / "consumer"
    stale_framework = target / "ls"
    stale_framework.mkdir(parents=True)
    (target / "localsetup.lock.json").write_text('{"version": 1}\n', encoding="utf-8")
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--source-root",
            str(root),
            "--home",
            str(home),
            "--target-directory",
            str(target),
            "audit-global-first",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    blocker_kinds = {blocker["kind"] for blocker in payload["blockers"]}
    assert {"stale_framework_source", "legacy_root_lockfile"} <= blocker_kinds
    assert payload["package_root"].endswith(".local/share/localsetup/packages")
    assert payload["registry_path"].endswith(".local/share/localsetup/registry.json")


def test_policy_blocks_high_risk_skill_in_strict_mode(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    skill_md = root / "ls" / "skills" / "ls-context" / "SKILL.md"
    skill_md.write_text(
        "---\nname: ls-context\ndescription: Context.\nrisk: high\npermissions: [filesystem-write]\n---\n",
        encoding="utf-8",
    )
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--repo",
            str(root),
            "--home",
            str(home),
            "install",
            "--yes",
            "--dependency-mode",
            "prompt-only",
            "--policy-mode",
            "strict",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["policy"]["blockers"]


def test_policy_blocks_invalid_risk_metadata_in_strict_mode(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    skill_md = root / "ls" / "skills" / "ls-context" / "SKILL.md"
    skill_md.write_text(
        "---\nname: ls-context\ndescription: Context.\nrisk: critical\n---\n",
        encoding="utf-8",
    )
    tool = root / "ls" / "tools" / "localsetup.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--repo",
            str(root),
            "--home",
            str(home),
            "install",
            "--yes",
            "--dependency-mode",
            "prompt-only",
            "--policy-mode",
            "strict",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert any("invalid skill policy metadata" in blocker for blocker in payload["policy"]["blockers"])


def test_sbom_command_writes_source_and_installed_boms(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home=home, packs=["core"]), home=home)
    tool = root / "ls" / "tools" / "localsetup.py"
    source_out = tmp_path / "source.cdx.json"
    installed_out = tmp_path / "installed.cdx.json"

    for args in (
        ["sbom", "--out", str(source_out)],
        ["sbom", "--installed", "--out", str(installed_out)],
    ):
        completed = subprocess.run(
            [sys.executable, str(tool), "--repo", str(root), "--home", str(home), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr + completed.stdout

    assert json.loads(source_out.read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"
    assert json.loads(installed_out.read_text(encoding="utf-8"))["components"]


def test_detect_invocation_target_prefers_git_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=project, text=True, capture_output=True, check=True)

    assert detect_invocation_target(nested) == project.resolve()

    outside = tmp_path / "outside"
    outside.mkdir()
    assert detect_invocation_target(outside) == outside.resolve()


def test_shell_registration_writes_managed_idempotent_shim_and_blocks_collision(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    first = register_shell_command(root, home=home, path_env="")
    second = register_shell_command(root, home=home, path_env=str(home / ".local" / "bin"))
    shim = home / ".local" / "bin" / "localsetup"

    assert first["managed"] is True
    assert first["path"]["on_path"] is False
    assert second["path"]["on_path"] is True
    assert is_managed_shim(shim)
    assert shell_registration_status(root, home=home, path_env="")["source_root"] == str(root.resolve())
    shim_text = shim.read_text(encoding="utf-8")
    assert 'LOCALSETUP_PROJECT_PYTHON="$LOCALSETUP_SOURCE_ROOT/.venv/bin/python"' in shim_text
    assert '"$LOCALSETUP_PROJECT_PYTHON" "$LOCALSETUP_TOOL" --help' in shim_text
    assert 'exec "$LOCALSETUP_PROJECT_PYTHON"' in shim_text
    assert "no usable Python runtime for LocalSetup" in shim_text
    assert "--sync-env --non-interactive --yes" in shim_text
    assert "uv --project" not in shim_text
    assert "run --locked" not in shim_text

    shim.write_text("#!/usr/bin/env bash\necho unmanaged\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unmanaged localsetup"):
        register_shell_command(root, home=home)



@pytest.mark.parametrize("runtime_branch", ["venv", "fallback"])
def test_managed_shim_dispatch_preserves_argv_for_each_python_branch(
    tmp_path: Path, runtime_branch: str
) -> None:
    source = make_temp_repo(tmp_path)
    root = tmp_path / "source checkout with spaces"
    source.rename(root)
    home = tmp_path / "home with spaces"
    shim = home / ".local" / "bin" / "localsetup"
    project_python = root / ".venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    project_args = tmp_path / "project-args.bin"
    fallback_args = tmp_path / "fallback-args.bin"
    real_python = shlex.quote(sys.executable)

    def python_runner(args_path: Path) -> str:
        return (
            "#!/usr/bin/env bash\n"
            'if [ "$#" -eq 2 ] && [ "$2" = "--help" ]; then\n'
            f'  exec {real_python} "$@"\n'
            "fi\n"
            f"printf '%s\\0' \"$@\" > {shlex.quote(str(args_path))}\n"
            f'exec {real_python} "$@"\n'
        )

    if runtime_branch == "venv":
        project_python.write_text(python_runner(project_args), encoding="utf-8")
        project_python.chmod(0o755)
    else:
        project_python.write_text("#!/usr/bin/env bash\nexit 126\n", encoding="utf-8")
        project_python.chmod(0o755)
    fallback_python = fallback_bin / "python3"
    fallback_python.write_text(python_runner(fallback_args), encoding="utf-8")
    fallback_python.chmod(0o755)
    register_shell_command(root, home=home, path_env=str(shim.parent))

    commands = (
        (["agent", "run", "--help"], "usage:"),
        (["llm", "complete", "--help"], "--request"),
        (["--source-root", str(root), "--home", str(home), "path", "source-root"], str(root)),
    )
    env = {**os.environ, "PATH": f"{fallback_bin}{os.pathsep}/usr/bin:/bin"}
    args_path = project_args if runtime_branch == "venv" else fallback_args

    for args, expected_output in commands:
        completed = subprocess.run(
            [str(shim), *args],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

        assert completed.returncode == 0, completed.stderr + completed.stdout
        output = completed.stdout.lower() if expected_output == "usage:" else completed.stdout
        assert expected_output in output
        forwarded = args_path.read_bytes().split(b"\0")[:-1]
        assert forwarded == [
            str(root / "ls" / "tools" / "localsetup.py").encode(),
            *(arg.encode() for arg in args),
        ]


def test_shell_registration_requires_exact_managed_marker(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    shim = home / ".local" / "bin" / "localsetup"
    shim.parent.mkdir(parents=True)
    shim.write_text(
        "#!/usr/bin/env bash\n# managed_by=localsetup-extra\nexport LOCALSETUP_GLOBAL_SHIM=1\n",
        encoding="utf-8",
    )

    assert is_managed_shim(shim) is False
    with pytest.raises(RuntimeError, match="unmanaged localsetup"):
        register_shell_command(root, home=home, path_env=str(shim.parent))


def test_shell_registration_warns_when_path_precedence_hides_shim(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    fake = earlier / "localsetup"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    result = register_shell_command(root, home=home, path_env=f"{earlier}:{home / '.local' / 'bin'}")

    assert result["path"]["on_path"] is True
    assert result["which"] == str(fake)
    assert any("before the managed shim" in warning for warning in result["warnings"])


def test_shell_registration_falls_back_when_project_python_is_bad(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    project_python = root / ".venv" / "bin" / "python"
    project_probe = tmp_path / "project-probe.txt"
    fallback_args = tmp_path / "fallback-args.txt"
    project_python.parent.mkdir(parents=True)
    project_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {shlex.quote(str(project_probe))}\nexit 126\n",
        encoding="utf-8",
    )
    project_python.chmod(0o755)
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    fallback_python = fallback_bin / "python3"
    fallback_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {shlex.quote(str(fallback_args))}\nexit 0\n",
        encoding="utf-8",
    )
    fallback_python.chmod(0o755)
    register_shell_command(root, home=home, path_env=str(home / ".local" / "bin"))

    completed = subprocess.run(
        [str(home / ".local" / "bin" / "localsetup"), "doctor"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fallback_bin}{os.pathsep}/usr/bin:/bin"},
    )

    assert completed.returncode == 0
    project_probe_text = project_probe.read_text(encoding="utf-8")
    assert "ls/tools/localsetup.py" in project_probe_text
    assert "--help" in project_probe_text
    fallback_text = fallback_args.read_text(encoding="utf-8")
    assert "ls/tools/localsetup.py" in fallback_text
    assert "doctor" in fallback_text


def test_shell_registration_suppresses_project_python_import_traceback(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    project_python = root / ".venv" / "bin" / "python"
    project_probe = tmp_path / "project-probe.txt"
    fallback_args = tmp_path / "fallback-args.txt"
    project_python.parent.mkdir(parents=True)
    project_python.write_text(
        (
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" > {shlex.quote(str(project_probe))}\n"
            "echo 'Traceback: missing yaml in project venv' >&2\n"
            "exit 1\n"
        ),
        encoding="utf-8",
    )
    project_python.chmod(0o755)
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    fallback_python = fallback_bin / "python3"
    fallback_python.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {shlex.quote(str(fallback_args))}\nexit 0\n",
        encoding="utf-8",
    )
    fallback_python.chmod(0o755)
    register_shell_command(root, home=home, path_env=str(home / ".local" / "bin"))

    completed = subprocess.run(
        [str(home / ".local" / "bin" / "localsetup"), "doctor"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fallback_bin}{os.pathsep}/usr/bin:/bin"},
    )

    assert completed.returncode == 0
    assert "Traceback" not in completed.stderr
    assert "--help" in project_probe.read_text(encoding="utf-8")
    assert "doctor" in fallback_args.read_text(encoding="utf-8")


def test_shell_registration_reports_repair_when_no_python_runtime_works(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    project_python = root / ".venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    project_python.write_text("#!/usr/bin/env bash\nexit 126\n", encoding="utf-8")
    project_python.chmod(0o755)
    fallback_bin = tmp_path / "fallback-bin"
    fallback_bin.mkdir()
    fallback_python = fallback_bin / "python3"
    fallback_python.write_text(
        "#!/usr/bin/env bash\necho 'Traceback: missing yaml' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fallback_python.chmod(0o755)
    register_shell_command(root, home=home, path_env=str(home / ".local" / "bin"))

    completed = subprocess.run(
        [str(home / ".local" / "bin" / "localsetup"), "doctor"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PATH": f"{fallback_bin}{os.pathsep}/usr/bin:/bin"},
    )

    assert completed.returncode == 2
    assert "no usable Python runtime for LocalSetup" in completed.stderr
    assert "--sync-env --non-interactive --yes" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_shell_registration_reports_error_and_status_edge_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.shell as shell_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"

    with pytest.raises(FileNotFoundError, match="missing LocalSetup source checkout"):
        register_shell_command(tmp_path / "missing-source", home=home)

    shim = home / ".local" / "bin" / "localsetup"
    register_shell_command(root, home=home, path_env=str(shim.parent))
    shim.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"# {shell_mod.SHIM_MARKER}",
                f"export {shell_mod.SHIM_ENV}=1",
                "LOCALSETUP_SOURCE_ROOT='unterminated",
            ]
        ),
        encoding="utf-8",
    )
    assert shell_mod._recorded_source_root(shim) == "'unterminated"

    original_read_text = Path.read_text

    def raise_for_shim(path: Path, *args: object, **kwargs: object) -> str:
        if path == shim:
            raise OSError("simulated unreadable shim")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raise_for_shim)
    assert is_managed_shim(shim) is False
    monkeypatch.setattr(Path, "read_text", original_read_text)

    earlier = tmp_path / "earlier"
    earlier.mkdir()
    fake = earlier / "localsetup"
    fake.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    status = shell_registration_status(root, home=home, path_env=f"{earlier}{os.pathsep}{shim.parent}")
    assert status["which"] == str(fake)
    assert any("before the managed shim" in warning for warning in status["warnings"])
