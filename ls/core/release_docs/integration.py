"""Explicit integration of an accepted documentation candidate, never force-push."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from .github import git


def _signing_identity(root: Path) -> str:
    key = git(root, "config", "--get", "user.signingkey")
    if not key or git(root, "config", "--get", "commit.gpgsign").lower() != "true":
        raise ValueError("A configured signing key and commit.gpgsign=true are required")
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", key):
        raise ValueError("The configured signing key must be a full OpenPGP fingerprint")
    if not git(root, "config", "--get", "user.name") or not git(root, "config", "--get", "user.email"):
        raise ValueError("A configured commit author is required")
    return key.upper()


def _verify_signed_commits(root: Path, before: str, expected_signer: str) -> None:
    commits = git(root, "rev-list", f"{before}..HEAD").splitlines()
    for commit in commits:
        result = subprocess.run(["git", "verify-commit", commit], cwd=root, capture_output=True, timeout=30)
        fingerprint = git(root, "show", "-s", "--format=%GF", commit).upper()
        if result.returncode or fingerprint != expected_signer:
            raise ValueError("Prepared commit lacks the configured valid signing fingerprint")


def integrate(root: Path, evidence: Path, *, push: bool = False) -> dict:
    from .application import apply_candidate
    from ..versioning import publish_preflight

    saved = json.loads(evidence.read_text())
    source = saved["plan"]["source_commit"]
    if git(root, "rev-parse", "HEAD") != source:
        raise ValueError("Candidate source changed; re-prepare")
    if push and git(root, "ls-remote", "origin", "refs/heads/main").split()[0] != source:
        raise ValueError("Remote main advanced; re-prepare without force-pushing")
    signer = _signing_identity(root)
    result = apply_candidate(root, saved["plan"], saved["candidate"])
    if not result["changed_paths"]:
        return {"ok": True, "head": source, "changed": False}
    subprocess.run(["git", "add", "--", *result["changed_paths"]], cwd=root, check=True)
    # Authored release preparation belongs to the already planned release.
    subprocess.run(["git", "commit", "-m", "docs: prepare release documentation", "-m", "Release-Type: none"],
                   cwd=root, check=True)
    baseline = saved["plan"].get("published_baseline", {}).get("commit") or saved["plan"].get("baseline_tag", source)
    prepared = publish_preflight(root, base=baseline, head="HEAD", fix=True)
    if not prepared["ok"]:
        raise ValueError("Canonical publication preflight failed after documentation preparation")
    _verify_signed_commits(root, source, signer)
    subprocess.run(["uv", "run", "--frozen", "python", "ls/tools/localsetup.py", "--source-root", ".",
                    "release-docs", "check"], cwd=root, check=True)
    subprocess.run(["uv", "run", "--frozen", "python", "ls/tools/docs_alignment.py", "--repo-root", ".",
                    "check", "--ci"], cwd=root, check=True)
    subprocess.run(["uv", "run", "--frozen", "python", "ls/tools/validate_branding.py", "--repo-root", ".",
                    "--strict"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    if git(root, "status", "--porcelain"):
        raise ValueError("Candidate validation changed tracked files; no push attempted")
    head = git(root, "rev-parse", "HEAD")
    if push:
        if git(root, "ls-remote", "origin", "refs/heads/main").split()[0] != source:
            raise ValueError("Remote main advanced; candidate retained locally, no push attempted")
        subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=root, check=True, timeout=120)
    return {"ok": True, "head": head, "changed": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    try:
        result = integrate(Path.cwd(), args.candidate, push=args.push)
        print(json.dumps(result))
        # GitHub output is a trusted exact commit, never model text.
        import os
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as stream:
                stream.write("head=" + result["head"] + "\n")
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
