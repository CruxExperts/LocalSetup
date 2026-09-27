import shutil
import subprocess
from pathlib import Path

from ls.core.versioning import SemVer


def copy_full_repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    adapter_roots = {source / ".cursor", source / ".opencode", source / ".kilo"}
    patterns = shutil.ignore_patterns(
        ".git",
        ".codex",
        ".omp",
        ".venv",
        ".venv-*",
        "__pycache__",
        ".pytest_cache",
        "localsetup.egg-info",
        "logs",
        "scrapling_output",
    )

    def ignore(directory, names):
        excluded = patterns(directory, names)
        if Path(directory) == source:
            excluded.update({".agents", ".localsetup-release.json"} & set(names))
        if Path(directory) in adapter_roots:
            excluded.update({"skills"} & set(names))
        if Path(directory) == source / ".localsetup-maint":
            excluded.update(set(names) - {"boundary.example.yaml"})
        return excluded
    shutil.copytree(source, repo, ignore=ignore)
    return repo


def run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=repo,
        text=True,
        capture_output=True,
        check=check,
    )


def init_git_repo(repo: Path, remote: Path) -> None:
    run(repo, "git", "init", "-q")
    run(repo, "git", "config", "user.email", "test@example.com")
    run(repo, "git", "config", "user.name", "Test User")
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "-m", "chore: initial", "--no-verify")
    run(repo, "git", "branch", "-M", "main")
    run(repo, "git", "remote", "add", "origin", str(remote))
    run(repo, "git", "push", "-u", "origin", "main", "--no-verify")


def repo_version(repo: Path) -> SemVer:
    return SemVer.parse((repo / "VERSION").read_text(encoding="utf-8").strip())


def next_patch_version(repo: Path) -> str:
    return str(repo_version(repo).bump("patch"))
