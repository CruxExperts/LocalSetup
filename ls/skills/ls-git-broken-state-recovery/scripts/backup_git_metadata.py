#!/usr/bin/env python3
# Purpose: Create and verify a Git metadata backup before manual repair.
# Created: 2026-09-02

"""
Create a verified Git metadata archive in an explicit controller task directory.
Usage: backup_git_metadata.py [REPO_PATH] --output-dir .agents/state/<task-slug>
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

PATH_MAX = 4096


def _task_output_dir(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > PATH_MAX:
        raise ValueError(f"output directory: invalid length or type (max {PATH_MAX})")
    path = Path(value.strip().strip("\x00")).expanduser().resolve()
    parts = path.parts
    try:
        agents_index = parts.index(".agents")
    except ValueError as exc:
        raise ValueError("output directory must be under .agents/state/<task-slug>") from exc
    if len(parts) <= agents_index + 2 or parts[agents_index + 1] != "state":
        raise ValueError("output directory must be under .agents/state/<task-slug>")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _repo_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > PATH_MAX:
        raise ValueError(f"repo path: invalid length or type (max {PATH_MAX})")
    path = Path(value.strip().strip("\x00")).resolve()
    if not path.is_dir():
        raise ValueError(f"repo path does not exist or is not a directory: {path}")
    return path


def _git_path(repo: Path, option: str) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", option],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or result.stdout or "unknown Git error").strip()
        raise ValueError(f"could not resolve {option}: {detail}")
    path = Path(result.stdout.strip()).resolve()
    if not path.is_dir():
        raise ValueError(f"resolved {option} is not a directory: {path}")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_roots(git_dir: Path, git_common_dir: Path) -> list[tuple[Path, str]]:
    if git_dir == git_common_dir:
        return [(git_common_dir, "git-common")]
    if _is_within(git_dir, git_common_dir):
        return [(git_common_dir, "git-common")]
    return [(git_common_dir, "git-common"), (git_dir, "git-worktree")]


def _required_archive_heads(git_dir: Path, git_common_dir: Path) -> set[str]:
    required = {"git-common/HEAD"}
    if git_dir != git_common_dir:
        if _is_within(git_dir, git_common_dir):
            relative_git_dir = git_dir.relative_to(git_common_dir).as_posix()
            required.add(f"git-common/{relative_git_dir}/HEAD")
        else:
            required.add("git-worktree/HEAD")
    return required


def create_verified_backup(repo: Path, task_output_dir: Path) -> dict[str, object]:
    git_dir = _git_path(repo, "--git-dir")
    git_common_dir = _git_path(repo, "--git-common-dir")
    if _is_within(task_output_dir, git_dir) or _is_within(task_output_dir, git_common_dir):
        raise ValueError("output directory must not be inside Git metadata")

    backup_dir = task_output_dir / "git-metadata-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    archive = backup_dir / f"git-metadata-{stamp}.tar.gz"
    temporary_archive = archive.with_suffix(archive.suffix + ".tmp")

    roots = _archive_roots(git_dir, git_common_dir)
    try:
        with tarfile.open(temporary_archive, mode="w:gz") as output:
            for source, archive_name in roots:
                output.add(source, arcname=archive_name, recursive=True)
        os.replace(temporary_archive, archive)
    finally:
        temporary_archive.unlink(missing_ok=True)

    required_heads = _required_archive_heads(git_dir, git_common_dir)
    with tarfile.open(archive, mode="r:gz") as stored:
        members = {member.name.rstrip("/") for member in stored.getmembers()}
    missing = sorted(required_heads - members)
    if missing:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"backup verification failed; missing archive members: {', '.join(missing)}")

    digest = _sha256(archive)
    receipt: dict[str, object] = {
        "schema_version": 1,
        "created_at": stamp,
        "repo": str(repo),
        "git_dir": str(git_dir),
        "git_common_dir": str(git_common_dir),
        "archive": str(archive),
        "archive_sha256": digest,
        "required_archive_members": sorted(required_heads),
        "verified": True,
    }
    receipt_path = archive.with_suffix(archive.suffix + ".json")
    receipt["receipt"] = str(receipt_path)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a verified Git metadata archive in an explicit controller task directory."
    )
    parser.add_argument("repo", nargs="?", default=".", help="Git work tree to back up")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Controller task directory under .agents/state/<task-slug>",
    )
    parser.add_argument("--json", action="store_true", help="Emit the verified receipt as JSON")
    args = parser.parse_args()

    try:
        repo = _repo_path(args.repo)
        task_output_dir = _task_output_dir(args.output_dir)
        receipt = create_verified_backup(repo, task_output_dir)
    except (OSError, RuntimeError, tarfile.TarError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(receipt, sort_keys=True))
    else:
        print("Verified Git metadata backup created.")
        print(f"Archive: {receipt['archive']}")
        print(f"SHA-256: {receipt['archive_sha256']}")
        print(f"Receipt: {receipt['receipt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
