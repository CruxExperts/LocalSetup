"""Export the optional S3 SDK graph without resolving or installing packages."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess


SKILLS = ("ls-backblaze", "ls-garage")
PACKAGES = {"boto3", "botocore", "jmespath", "s3transfer", "python-dateutil", "six", "urllib3"}


def export(root: Path) -> bytes:
    """Keep hashes and markers from uv's authoritative locked group export."""
    result = subprocess.run(
        ["uv", "export", "--locked", "--offline", "--only-group", "s3-sdk",
         "--no-header", "--no-annotate", "--no-emit-project", "--format", "requirements.txt"],
        cwd=root, capture_output=True, check=True,
    )
    data = result.stdout
    entries = [line for line in data.decode("utf-8").splitlines() if "==" in line]
    names = {line.split("==", 1)[0] for line in entries}
    if names != PACKAGES or not any(line.startswith("boto3==1.43.108 ") for line in entries):
        raise ValueError("S3 SDK export does not match the reviewed dependency graph")
    blocks = data.decode("utf-8").replace("\\\n", "").splitlines()
    if len(blocks) != len(PACKAGES) or any(
        not re.fullmatch(r"[a-z0-9-]+==[^\s]+(?:\s*;[^\n]*?)?\s+(?:--hash=sha256:[a-f0-9]{64}\s*)+", block)
        for block in blocks
    ):
        raise ValueError("S3 SDK export is missing distribution hashes")
    return data


def refresh(root: Path, *, check: bool) -> list[str]:
    data = export(root)
    targets = [root / "ls/skills" / skill / "requirements-s3-sdk.txt" for skill in SKILLS]
    for path in targets:
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("S3 SDK export target must not contain symlinks")
        if path.exists() and not path.is_file():
            raise ValueError("S3 SDK export target must be a regular file")
    changed = [str(path.relative_to(root)) for path in targets if not path.exists() or path.read_bytes() != data]
    if not check:
        for path in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            if str(path.relative_to(root)) in changed:
                path.write_bytes(data)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = refresh(args.repo_root.absolute(), check=args.check)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("S3 SDK export failed; verify the locked s3-sdk group and regular output paths")
        return 2
    if changed:
        print(("Stale: " if args.check else "Updated: ") + ", ".join(changed))
    return 1 if args.check and changed else 0
