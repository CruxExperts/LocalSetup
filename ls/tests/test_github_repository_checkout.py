from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

import ls.core.github_repo.checkout as checkout_module
from ls.core.github_repo.model import RepositoryTarget


_TARGET = RepositoryTarget(hostname="github.com", owner="Owner", repository="Repo")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Checkout Test")
    _git(root, "config", "user.email", "checkout-test@example.invalid")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    return _git(root, "rev-parse", "HEAD")


def _track_snapshot_git_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, object]]]:
    calls: list[tuple[list[str], dict[str, object]]] = []
    real_popen = checkout_module.subprocess.Popen

    def recording_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        calls.append((list(args), dict(kwargs)))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(checkout_module.subprocess, "Popen", recording_popen)
    return calls


def test_snapshot_redacts_remote_credentials_and_uses_read_only_local_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "private-checkout-root"
    head = _repository(root)
    secret = "remote-user:very-secret-token"
    _git(root, "remote", "add", "origin", f"https://{secret}@github.com/Owner/Repo.git")
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    _git(root, "branch", "--set-upstream-to=origin/main", "main")
    index_path = root / ".git" / "index"
    before_index = (index_path.read_bytes(), index_path.stat().st_mtime_ns)
    calls = _track_snapshot_git_calls(monkeypatch)

    result = checkout_module.snapshot_checkout(root, _TARGET)

    assert result["supported"] is True
    assert result["target"] == "Owner/Repo"
    assert result["origin"] == {"configured": True, "matches_target": True}
    assert result["upstream"] == {
        "configured": True,
        "remote": "origin",
        "branch": "main",
        "matches_target": True,
    }
    assert result["head"] == head
    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert "https://" not in serialized
    assert str(root) not in serialized
    assert "tracked.txt" not in serialized
    assert (index_path.read_bytes(), index_path.stat().st_mtime_ns) == before_index

    read_commands = {"rev-parse", "symbolic-ref", "status", "for-each-ref", "config"}
    assert calls
    for argv, kwargs in calls:
        command_index = 6  # git, replacement-ref guard, and two fixed -c settings
        assert argv[0] == "git"
        assert argv[command_index] in read_commands
        assert "fetch" not in argv and "push" not in argv and "ls-remote" not in argv
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"  # type: ignore[index]


def test_snapshot_reports_missing_upstream_and_dirty_categories_without_paths(tmp_path: Path) -> None:
    root = tmp_path / "dirty-checkout"
    _repository(root)
    (root / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (root / "untracked-secret-name.txt").write_text("new\n", encoding="utf-8")

    first = checkout_module.snapshot_checkout(root, _TARGET)
    second = checkout_module.snapshot_checkout(root, _TARGET)

    assert first["supported"] is True
    assert first["branch"] == {"detached": False, "name": "main"}
    assert first["origin"] == {"configured": False, "matches_target": False}
    assert first["upstream"] == {
        "configured": False,
        "remote": None,
        "branch": None,
        "matches_target": False,
    }
    assert first["dirty"] == {
        "clean": False,
        "staged_count": 1,
        "worktree_count": 1,
        "untracked_count": 1,
        "status_digest": first["dirty"]["status_digest"],
    }
    assert first["dirty"]["status_digest"] == second["dirty"]["status_digest"]
    serialized = json.dumps(first, sort_keys=True)
    assert str(root) not in serialized
    assert "staged.txt" not in serialized
    assert "tracked.txt" not in serialized
    assert "untracked-secret-name.txt" not in serialized


def test_snapshot_counts_worktree_type_changes(tmp_path: Path) -> None:
    root = tmp_path / "type-change-checkout"
    _repository(root)
    tracked = root / "tracked.txt"
    tracked.unlink()
    tracked.symlink_to("replacement.txt")

    result = checkout_module.snapshot_checkout(root, _TARGET)

    assert result["supported"] is True
    assert result["dirty"]["clean"] is False
    assert result["dirty"]["worktree_count"] == 1


def test_snapshot_reports_detached_head(tmp_path: Path) -> None:
    root = tmp_path / "detached-checkout"
    head = _repository(root)
    _git(root, "checkout", "--detach", head)

    result = checkout_module.snapshot_checkout(root, _TARGET)

    assert result["supported"] is True
    assert result["head"] == head
    assert result["branch"] == {"detached": True, "name": None}
    assert result["upstream"] == {
        "configured": False,
        "remote": None,
        "branch": None,
        "matches_target": False,
    }


@pytest.mark.parametrize(
    ("path_kind", "expected_reason"),
    [("missing", "checkout_missing"), ("non_git", "not_git_repository"), ("unborn", "invalid_git_state")],
)
def test_snapshot_safe_failures_do_not_include_local_paths(
    tmp_path: Path,
    path_kind: str,
    expected_reason: str,
) -> None:
    root = tmp_path / f"private-{path_kind}-path"
    if path_kind == "non_git":
        root.mkdir()
    elif path_kind == "unborn":
        root.mkdir()
        _git(root, "init", "--initial-branch=main")

    result = checkout_module.snapshot_checkout(root, _TARGET)

    assert result == {"supported": False, "reason": expected_reason}
    assert str(root) not in json.dumps(result)


def test_snapshot_rejects_invalid_repository_target_safely(tmp_path: Path) -> None:
    result = checkout_module.snapshot_checkout(tmp_path, "Owner/Repo")  # type: ignore[arg-type]

    assert result == {"supported": False, "reason": "invalid_repository_target"}
