import hashlib
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "ls" / "skills" / "ls-git-broken-state-recovery"
SNAPSHOT = SKILL / "scripts" / "snapshot_git_state.py"
BACKUP = SKILL / "scripts" / "backup_git_metadata.py"
GUIDED = SKILL / "scripts" / "guided_repair_plan.py"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


def make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    run("git", "init", "-q", "-b", "main", cwd=repo)
    run("git", "config", "user.name", "Recovery Test", cwd=repo)
    run("git", "config", "user.email", "recovery@example.invalid", cwd=repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("git", "add", "seed.txt", cwd=repo)
    run("git", "commit", "-q", "-m", "seed", cwd=repo)
    return repo


def task_output(root: Path) -> Path:
    return root / ".agents" / "state" / "slice-021-test"


def capture_snapshot(repo: Path, output: Path) -> dict[str, object]:
    result = run(
        sys.executable,
        str(SNAPSHOT),
        str(repo),
        "--output-dir",
        str(output),
        "--json",
        cwd=ROOT,
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def test_snapshot_uses_explicit_controller_task_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)

    missing_output = run(sys.executable, str(SNAPSHOT), str(repo), cwd=ROOT, check=False)
    assert missing_output.returncode == 2
    assert "--output-dir" in missing_output.stderr

    invalid_output = run(
        sys.executable,
        str(SNAPSHOT),
        str(repo),
        "--output-dir",
        str(tmp_path / "ordinary-output"),
        cwd=ROOT,
        check=False,
    )
    assert invalid_output.returncode == 2
    assert ".agents/state/<task-slug>" in invalid_output.stderr

    output = task_output(tmp_path)
    payload = capture_snapshot(repo, output)
    snapshot_dir = Path(str(payload["snapshot_dir"]))
    assert snapshot_dir.is_relative_to(output / "git-state-snapshots")
    assert not (repo / ".git-state-snapshots").exists()
    assert json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8")) == payload
    status = (snapshot_dir / "status.txt").read_text(encoding="utf-8")
    assert "status --porcelain=v2 --branch" in status


def test_detached_head_requires_all_three_corroborating_signals(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    run("git", "checkout", "-q", "--detach", cwd=repo)
    output = task_output(tmp_path)
    payload = capture_snapshot(repo, output)
    snapshot_dir = Path(str(payload["snapshot_dir"]))

    detected = run(
        sys.executable,
        str(GUIDED),
        "--snapshot",
        str(snapshot_dir),
        cwd=ROOT,
    )
    assert "[detached-head-state]" in detected.stdout
    assert "porcelain v2" in detected.stdout

    (snapshot_dir / "symbolic_ref_head.txt").write_text(
        "# symbolic_ref_head\n# command: git symbolic-ref -q HEAD\n# exit_code: 0\n\nrefs/heads/main\n",
        encoding="utf-8",
    )
    not_corroborated = run(
        sys.executable,
        str(GUIDED),
        "--snapshot",
        str(snapshot_dir),
        cwd=ROOT,
    )
    assert "[detached-head-state]" not in not_corroborated.stdout
    assert "No deterministic symptom match" in not_corroborated.stderr


def test_missing_ref_plan_rescues_before_force_and_requires_confirmation() -> None:
    result = run(
        sys.executable,
        str(GUIDED),
        "--symptom",
        "missing-or-broken-refs",
        cwd=ROOT,
    )
    plan = result.stdout
    reflog = plan.index("git reflog")
    rescue = plan.index("git branch rescue/")
    remote = plan.index("git show-ref --verify refs/remotes/origin/<branch>")
    confirmation = plan.index("POINT OF RISK")
    force = plan.index("git branch -f")
    assert reflog < rescue < remote < confirmation < force
    assert "repository, local branch, verified remote ref, and\nresolved commit" in plan


def test_linked_worktree_backup_archives_and_verifies_real_metadata(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    run("git", "branch", "linked", cwd=repo)
    linked = tmp_path / "linked-worktree"
    run("git", "worktree", "add", "-q", str(linked), "linked", cwd=repo)
    output = task_output(tmp_path)

    result = run(
        sys.executable,
        str(BACKUP),
        str(linked),
        "--output-dir",
        str(output),
        "--json",
        cwd=ROOT,
    )
    receipt = json.loads(result.stdout)
    archive = Path(receipt["archive"])
    receipt_path = Path(receipt["receipt"])
    assert receipt["verified"] is True
    assert Path(receipt["git_dir"]) != Path(receipt["git_common_dir"])
    assert archive.is_relative_to(output)
    assert receipt_path.is_relative_to(output)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["archive_sha256"]

    with tarfile.open(archive, "r:gz") as stored:
        members = {member.name.rstrip("/") for member in stored.getmembers()}
    assert set(receipt["required_archive_members"]) <= members
    assert "git-common/HEAD" in members
    assert any(name.startswith("git-common/worktrees/") and name.endswith("/HEAD") for name in members)


def test_manual_head_plan_creates_verified_backup_before_showing_repairs(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    output = task_output(tmp_path)
    payload = capture_snapshot(repo, output)
    snapshot_dir = Path(str(payload["snapshot_dir"]))
    head_path = Path(str(payload["git_dir"])) / "HEAD"
    original_head = head_path.read_bytes()

    (snapshot_dir / "branch_current.txt").write_text(
        "# branch_current\n# command: git branch --show-current\n# exit_code: 0\n\nother-branch\n",
        encoding="utf-8",
    )
    result = run(
        sys.executable,
        str(GUIDED),
        "--snapshot",
        str(snapshot_dir),
        "--repo",
        str(repo),
        "--output-dir",
        str(output),
        cwd=ROOT,
    )

    assert "Verified metadata backup:" in result.stdout
    assert "explicit point-of-risk confirmation" in result.stdout
    quoted_repo = shlex.quote(str(repo))
    assert f"git -C {quoted_repo} branch --show-current" in result.stdout
    assert (
        f"git -C {quoted_repo} symbolic-ref HEAD refs/heads/<expected-branch>"
        in result.stdout
    )
    assert str(head_path) in result.stdout
    assert list((output / "git-metadata-backups").glob("*.tar.gz"))
    assert list((output / "git-metadata-backups").glob("*.tar.gz.json"))
    assert head_path.read_bytes() == original_head


def test_existing_snapshot_rejects_a_different_task_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    output = task_output(tmp_path)
    payload = capture_snapshot(repo, output)
    snapshot_dir = Path(str(payload["snapshot_dir"]))
    other_output = tmp_path / ".agents" / "state" / "different-slice"

    result = run(
        sys.executable,
        str(GUIDED),
        "--snapshot",
        str(snapshot_dir),
        "--repo",
        str(repo),
        "--output-dir",
        str(other_output),
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 1
    assert "--output-dir must match the snapshot task output directory" in result.stderr
    assert "git symbolic-ref HEAD" not in result.stdout
    assert not (other_output / "git-metadata-backups").exists()


def test_existing_snapshot_rejects_a_different_repository(tmp_path: Path) -> None:
    source_repo = make_repo(tmp_path, "source")
    other_repo = make_repo(tmp_path, "other")
    output = task_output(tmp_path)
    payload = capture_snapshot(source_repo, output)
    snapshot_dir = Path(str(payload["snapshot_dir"]))

    result = run(
        sys.executable,
        str(GUIDED),
        "--snapshot",
        str(snapshot_dir),
        "--repo",
        str(other_repo),
        "--output-dir",
        str(output),
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 1
    assert "snapshot does not belong to the selected repository" in result.stderr
    assert "git symbolic-ref HEAD" not in result.stdout
    assert not (output / "git-metadata-backups").exists()


def test_manual_head_plan_withholds_repairs_without_verified_backup() -> None:
    result = run(
        sys.executable,
        str(GUIDED),
        "--symptom",
        "head-ref-disagreement",
        cwd=ROOT,
    )
    assert "Manual HEAD repair commands are withheld" in result.stdout
    assert "git symbolic-ref HEAD refs/heads/<expected-branch>" not in result.stdout
    assert "printf '%s" not in result.stdout
