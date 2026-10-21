import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ls.core.manifests import load_platforms


def load_create_sandbox_module(script: Path | None = None):
    root = Path(__file__).resolve().parents[2]
    script = script or root / "ls" / "skills" / "ls-skill-sandbox-tester" / "scripts" / "create_sandbox.py"
    spec = importlib.util.spec_from_file_location(f"ls_skill_sandbox_create_sandbox_{id(script)}", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_skill_root_subpaths_follow_platform_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    module = load_create_sandbox_module()
    platform_roots = {
        repo_path
        for platform in load_platforms(root)
        for repo_path in platform.repo_paths
    }

    assert set(module.SKILL_ROOT_SUBPATHS) == {"ls/skills", *platform_roots}
    assert "skills" not in module.SKILL_ROOT_SUBPATHS
    assert ".agents/skills" in module.SKILL_ROOT_SUBPATHS


def test_resolves_skill_name_from_framework_source(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    module = load_create_sandbox_module()

    monkeypatch.chdir(root)

    resolved = module._resolve_skill_dir_by_name("ls-create-agent-skills", None)
    assert resolved == root / "ls" / "skills" / "ls-create-agent-skills"


def test_resolves_skill_name_from_adapter_root(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    adapter_skill = repo / ".agents" / "skills" / "ls-demo"
    adapter_skill.mkdir(parents=True)
    (adapter_skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n",
        encoding="utf-8",
    )
    module = load_create_sandbox_module()

    monkeypatch.chdir(repo)

    resolved = module._resolve_skill_dir_by_name("ls-demo", None)
    assert resolved == adapter_skill


def test_projection_rejects_symlink_and_oversized_files(tmp_path: Path) -> None:
    module = load_create_sandbox_module()
    target = tmp_path / "platforms-target.yaml"
    target.write_text('platforms:\n  - id: codex\n    repo_paths: [".agents/skills"]\n', encoding="utf-8")
    symlink = tmp_path / "platforms-symlink.yaml"
    symlink.symlink_to(target)
    oversized = tmp_path / "platforms-oversized.yaml"
    oversized.write_text("x" * (module.PLATFORM_PROJECTION_MAX + 1), encoding="utf-8")

    with pytest.raises(ValueError, match="symlinked"):
        module._projection_skill_roots(symlink)
    with pytest.raises(ValueError, match="oversized"):
        module._projection_skill_roots(oversized)


@pytest.mark.parametrize(
    "repo_paths",
    [
        "[malformed",
        '".agents/skills"',
        '["/tmp/skills"]',
        '["~/.agents/skills"]',
        '["../escape/skills"]',
        '[".agents/rules"]',
    ],
    ids=["malformed", "non-list", "absolute", "home", "traversal", "wrong-suffix"],
)
def test_projection_rejects_malformed_or_unsafe_repo_paths(tmp_path: Path, repo_paths: str) -> None:
    module = load_create_sandbox_module()
    projection = tmp_path / "platforms.yaml"
    projection.write_text(f"platforms:\n  - id: unsafe\n    repo_paths: {repo_paths}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        module._projection_skill_roots(projection)


def test_projection_failure_uses_deterministic_standalone_fallback(tmp_path: Path, monkeypatch) -> None:
    module = load_create_sandbox_module()
    malformed = tmp_path / "platforms.yaml"
    malformed.write_text("repo_paths: [malformed\n", encoding="utf-8")
    monkeypatch.setattr(module, "_projection_candidates", lambda: [malformed])

    first = module._skill_root_subpaths()
    second = module._skill_root_subpaths()

    assert first == module.FALLBACK_SKILL_ROOT_SUBPATHS
    assert second == first
    assert ".agents/skills" in first
    assert ".codex/skills" not in first


def test_standalone_copied_skill_resolves_current_adapter_and_creates_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    source = root / "ls" / "skills" / "ls-skill-sandbox-tester"
    standalone = tmp_path / "standalone" / "ls-skill-sandbox-tester"
    shutil.copytree(source, standalone)
    repo = tmp_path / "repo"
    adapter_skill = repo / ".agents" / "skills" / "ls-demo"
    adapter_skill.mkdir(parents=True)
    (adapter_skill / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    sandbox_base = tmp_path / "sandboxes"
    sandbox_base.mkdir()
    monkeypatch.delenv("LOCALSETUP_FRAMEWORK_DIR", raising=False)

    completed = subprocess.run(
        [
            sys.executable,
            str(standalone / "scripts" / "create_sandbox.py"),
            "--skill-name",
            "ls-demo",
            "--base-dir",
            str(sandbox_base),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    copied_skill = Path(completed.stdout.strip())
    assert copied_skill.parent.parent == sandbox_base
    assert (copied_skill.parent / ".localsetup-sandbox.json").is_file()
    assert (copied_skill / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: ls-demo")
