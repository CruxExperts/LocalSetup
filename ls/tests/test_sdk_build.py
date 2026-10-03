from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib

import pytest
from setuptools import Distribution

from ls.core.sdk_payload.build import BuildSDK
from ls.core.sdk_payload.integrity import verify

ROOT = Path(__file__).resolve().parents[2]


def _hardlink_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def _copy_sdist_fixture(source_root: Path, fixture_root: Path) -> None:
    for name in ("LICENSE", "MANIFEST.in", "README.md", "VERSION", "pyproject.toml", "uv.lock"):
        _hardlink_or_copy(str(source_root / name), str(fixture_root / name))
    for name in ("assets", "ls", "vendor"):
        shutil.copytree(
            source_root / name,
            fixture_root / name,
            copy_function=_hardlink_or_copy,
            ignore=shutil.ignore_patterns("__pycache__", ".cache", ".pytest_cache", ".agents", "local-context"),
            symlinks=True,
        )

    # Keep exclusion probes in the fixture without copying private checkout data.
    local_context = fixture_root / "ls" / "docs" / "local-context"
    local_context.mkdir(parents=True)
    (local_context / "private-fixture.md").write_text("private fixture content\n", encoding="utf-8")
    version_record = fixture_root / "ls" / "docs" / "releases" / "4.44.1.json"
    version_record.parent.mkdir(parents=True, exist_ok=True)
    if version_record.exists() or version_record.is_symlink():
        version_record.unlink()
    version_record.write_text('{"fixture": "must not ship"}\n', encoding="utf-8")


def command(tmp_path: Path) -> BuildSDK:
    distribution = Distribution({"packages": ["ls"], "package_dir": {"ls": str(ROOT / "ls")}})
    distribution.script_name = "setup.py"
    build = BuildSDK(distribution)
    build.ensure_finalized()
    build.build_lib = str(tmp_path / "output")
    return build


def test_build_retains_exact_private_payload_and_repeats_cleanly(tmp_path):
    build = command(tmp_path)
    build.run()
    private = Path(build.build_lib) / "ls" / "_sdk_payload"
    assert verify(private) == verify(ROOT / "vendor/lscli")
    assert not (Path(build.build_lib) / "pydantic_ai").exists()
    assert str(private / "manifest.json") in build.get_outputs()
    build.run()


@pytest.mark.parametrize("mutation", ["extra", "changed", "symlink"])
def test_reused_build_output_rejects_substitution(tmp_path, mutation):
    build = command(tmp_path)
    build.run()
    private = Path(build.build_lib) / "ls" / "_sdk_payload"
    if mutation == "extra":
        (private / "extra.py").write_text("pass")
    elif mutation == "changed":
        (private / "pydantic_ai/__init__.py").write_text("changed")
    else:
        (private / "extra").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        build.run()


def test_editable_framework_build_does_not_supply_sdk(tmp_path):
    build = command(tmp_path)
    build.editable_mode = True
    build.run()
    assert not (Path(build.build_lib) / "ls" / "_sdk_payload").exists()


def test_core_preserves_lazy_main_export_without_runtime_imports():
    code = "import sys; sys.path.insert(0, sys.argv[1]); import ls.core; from ls.core.sdk_payload.integrity import verify; assert 'ls.core.cli' not in sys.modules; assert 'yaml' not in sys.modules"
    subprocess.run([sys.executable, "-I", "-S", "-c", code, str(ROOT)], check=True)
    import ls.core
    from ls.core.cli import main

    assert ls.core.main is main


def test_reused_build_rejects_newer_modified_dependency_lock(tmp_path):
    import os
    build = command(tmp_path)
    build.run()
    target = Path(build.build_lib) / 'ls/config/sdk-runtime.lock'
    target.write_bytes(b'changed==1')
    os.utime(target, (4102444800, 4102444800))
    with pytest.raises(ValueError, match='Stale dependency build output'):
        build.run()


def test_sdist_retains_nested_reference_docs_and_supports_portable_all_install(tmp_path):
    fixture = tmp_path / "source"
    fixture.mkdir()
    _copy_sdist_fixture(ROOT, fixture)
    output = tmp_path / "sdist"
    output.mkdir()

    build_system = tomllib.loads((fixture / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    setuptools_requirement = next(
        requirement for requirement in build_system["requires"] if requirement.startswith("setuptools==")
    )
    expected_setuptools = setuptools_requirement.split("==", 1)[1]
    build_script = (
        "import setuptools, sys; "
        "from setuptools.build_meta import build_sdist; "
        "assert setuptools.__version__ == sys.argv[2], "
        "f'expected pinned setuptools {sys.argv[2]}, got {setuptools.__version__}'; "
        "print(build_sdist(sys.argv[1]))"
    )
    build_env = os.environ.copy()
    build_env.pop("PYTHONPATH", None)
    build_env["PYTHONDONTWRITEBYTECODE"] = "1"
    built = subprocess.run(
        [sys.executable, "-c", build_script, str(output), expected_setuptools],
        cwd=fixture,
        env=build_env,
        capture_output=True,
        text=True,
        check=True,
    )
    archive_name = built.stdout.strip().splitlines()[-1]
    archive_path = output / archive_name
    assert archive_path.is_file(), built.stdout + built.stderr

    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        assert len(roots) == 1
        archive_root = roots.pop()
        prefix = f"{archive_root}/"
        names = {member.name.removeprefix(prefix) for member in members if member.name.startswith(prefix)}

        bootstrap_docs = sorted(
            path for path in (ROOT / "ls" / "docs" / "bootstrap-packs").rglob("*") if path.is_file()
        )
        assert bootstrap_docs
        for source in bootstrap_docs:
            relative = source.relative_to(ROOT).as_posix()
            member = archive.getmember(prefix + relative)
            assert member.isfile(), relative
            contents = archive.extractfile(member)
            assert contents is not None
            assert contents.read() == source.read_bytes(), relative

        release_path = "ls/docs/releases/4.4.0.md"
        release_member = archive.getmember(prefix + release_path)
        release_contents = archive.extractfile(release_member)
        assert release_member.isfile()
        assert release_contents is not None
        assert release_contents.read() == (ROOT / release_path).read_bytes()

        assert not any(
            name == "ls/docs/local-context" or name.startswith("ls/docs/local-context/") for name in names
        )
        assert "ls/docs/releases/4.44.1.json" not in names

        # Validate the candidate archive before extraction; extraction remains
        # confined to this temporary directory and rejects links/special files.
        total_size = 0
        for member in members:
            parts = Path(member.name).parts
            assert not Path(member.name).is_absolute()
            assert all(part not in {"", ".", ".."} for part in parts)
            assert member.isdir() or member.isfile()
            total_size += member.size if member.isfile() else 0
        assert len(members) <= 20000
        assert total_size <= 256 * 1024 * 1024
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        archive.extractall(extracted, filter="data")

    source_root = extracted / archive_root
    home = tmp_path / "home"
    target = tmp_path / "target"
    home.mkdir()
    target.mkdir()
    for directory in ("config", "data", "state", "cache"):
        (home / ".localsetup-xdg" / directory).mkdir(parents=True, exist_ok=True)

    install_env = os.environ.copy()
    install_env.pop("PYTHONPATH", None)
    for key in tuple(install_env):
        if key.startswith("LOCALSETUP_") or key.startswith("LS_"):
            install_env.pop(key)
    install_env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".localsetup-xdg" / "config"),
            "XDG_DATA_HOME": str(home / ".localsetup-xdg" / "data"),
            "XDG_STATE_HOME": str(home / ".localsetup-xdg" / "state"),
            "XDG_CACHE_HOME": str(home / ".localsetup-xdg" / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    installed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(source_root / "ls" / "tools" / "localsetup.py"),
            "--source-root",
            str(source_root),
            "--home",
            str(home),
            "install",
            "--target-directory",
            str(target),
            "--preset",
            "all",
            "--mode",
            "portable",
            "--dependency-mode",
            "prompt-only",
            "--apply",
        ],
        cwd=tmp_path,
        env=install_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    materialized_bootstrap_doc = (
        home
        / ".local"
        / "share"
        / "localsetup"
        / "packages"
        / "ls-workflow-queue-batch-implement"
        / "references"
        / "localsetup"
        / "docs"
        / "bootstrap-packs"
        / "codex-agent-team"
        / "README.md"
    )
    expected_bootstrap_doc = source_root / "ls" / "docs" / "bootstrap-packs" / "codex-agent-team" / "README.md"
    materialized_text = materialized_bootstrap_doc.read_text(encoding="utf-8")
    expected_text = expected_bootstrap_doc.read_text(encoding="utf-8")
    assert "# Codex Agent Team Bootstrap Pack" in materialized_text
    assert "This pack captures the generic Codex controller/subagent workflow" in expected_text
    assert "This pack captures the generic Codex controller/subagent workflow" in materialized_text
    assert "references/localsetup/docs/bootstrap-packs/codex-agent-team/AUDIT_PROMPT.md" in materialized_text
