"""Deterministic validation of release-document candidates."""
from __future__ import annotations

import re
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ls.core.git_subprocess import run_git

from .content import validate_record
from .render import render_outputs


_SHA = re.compile(r"[0-9a-f]{40}\Z")
_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
_POST_SOURCE_FILES = {
    "README.md", "ls/README.md", "assets/README.md",
}


def _post_source_document(path: str) -> bool:
    return path in _POST_SOURCE_FILES or path.startswith("ls/docs/") and path.endswith((".md", ".json"))


def _version_only_sync(root: Path, source: str, path: str, version: str) -> bool:
    if path == "VERSION":
        committed = run_git(root, ["show", "HEAD:VERSION"], text=True, capture_output=True, check=False)
        return committed.returncode == 0 and committed.stdout == f"{version}\n"
    before = run_git(root, ["show", f"{source}:{path}"], text=True, capture_output=True, check=False)
    after = run_git(root, ["show", f"HEAD:{path}"], text=True, capture_output=True, check=False)
    if before.returncode or after.returncode:
        return False
    if path == "pyproject.toml":
        expected = re.sub(r'(?m)^version = "[0-9]+\.[0-9]+\.[0-9]+"$',
                          f'version = "{version}"', before.stdout)
    elif path == "uv.lock":
        expected = re.sub(r'(?m)(^\[\[package\]\]\nname = "localsetup"\nversion = ")[0-9]+\.[0-9]+\.[0-9]+(")',
                          rf'\g<1>{version}\2', before.stdout)
    else:
        return False
    return after.stdout == expected


def _finding(code: str, message: str, path: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if path:
        result["path"] = path
    return result


def _git_exists(root: Path, args: list[str]) -> bool:
    return run_git(root, args, text=True, capture_output=True, check=False).returncode == 0


def _source_evidence_exists(root: Path, record: dict[str, Any], evidence: str) -> bool:
    if _SHA.fullmatch(evidence):
        return (_git_exists(root, ["cat-file", "-e", f"{evidence}^{{commit}}"])
                and _git_exists(root, ["merge-base", "--is-ancestor", evidence, record["source_commit"]]))
    if _git_exists(root, ["cat-file", "-e", f"{record['source_commit']}:{evidence}"]):
        return True
    # A removed path remains valid release evidence when the verified baseline
    # contains it and the committed release diff records its deletion.
    if not _git_exists(root, ["cat-file", "-e", f"{record['baseline_tag']}:{evidence}"]):
        return False
    diff = run_git(root, ["diff", "--name-only", "--diff-filter=D", record["baseline_tag"],
                          record["source_commit"], "--", evidence], text=True, capture_output=True, check=False)
    return diff.returncode == 0 and evidence in diff.stdout.splitlines()


def _anchor_slug(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9 -]", "", value.lower()).replace(" ", "-")).strip("-")


def _anchors(text: str) -> tuple[set[str], set[str]]:
    anchors: set[str] = set()
    duplicates: set[str] = set()
    fence: str | None = None
    for line in text.splitlines():
        if match := re.match(r"^\s*(```+|~~~+)", line):
            marker = match.group(1)[0]
            fence = None if fence == marker else marker
            continue
        if fence:
            continue
        if match := re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line):
            anchor = _anchor_slug(match.group(1))
            if anchor in anchors:
                duplicates.add(anchor)
            anchors.add(anchor)
    return anchors, duplicates


def _destination(source: str, raw: str) -> str | None:
    decoded = unquote(raw).strip()
    if not decoded:
        return source
    if decoded.startswith("/"):
        return None
    destination = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
    if destination in {"..", "."} or destination.startswith("../"):
        return None
    return destination


def _check_links(root: Path, outputs: dict[str, str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path, text in outputs.items():
        _, duplicates = _anchors(text)
        for anchor in sorted(duplicates):
            findings.append(_finding("duplicate_anchor", f"Duplicate heading anchor: {anchor}", path))
        for target in _LINK.findall(text):
            target = unquote(target.strip().strip("<>"))
            if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue
            raw, _, anchor = target.partition("#")
            destination = _destination(path, raw)
            if destination is None:
                findings.append(_finding("unsafe_link", f"Link escapes the repository: {target}", path))
                continue
            if destination not in outputs and not (root / destination).is_file():
                findings.append(_finding("broken_link", f"Link target does not exist: {target}", path))
                continue
            if anchor:
                content = outputs[destination] if destination in outputs else (root / destination).read_text(encoding="utf-8")
                anchors, _ = _anchors(content)
                if _anchor_slug(anchor) not in anchors:
                    findings.append(_finding("broken_anchor", f"Link anchor does not exist: {target}", path))
    return findings


def check(root: Path, record: dict[str, Any], expected_commit: str | None = None,
          target_version: str | None = None) -> dict[str, Any]:
    """Check a record and rendered candidate without modifying the source tree."""
    repo_root = Path(root)
    findings: list[dict[str, str]] = []
    try:
        record = validate_record(record)
    except ValueError as exc:
        return {"ok": False, "findings": [_finding("invalid_record", str(exc))]}
    if expected_commit is not None and record["source_commit"] != expected_commit:
        findings.append(_finding("source_commit_mismatch", "Release record source_commit differs from expected commit."))
    if target_version is not None and record["version"] != target_version:
        findings.append(_finding("target_version_mismatch", "Release record version differs from the planned target version."))
    if target_version is None:
        version_path = repo_root / "VERSION"
        current_version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
        if current_version != record["version"]:
            findings.append(_finding("version_mismatch", "Release record version differs from committed VERSION."))
    if not _git_exists(repo_root, ["cat-file", "-e", f"{record['source_commit']}^{{commit}}"]):
        findings.append(_finding("missing_source_commit", "Release record source_commit is unavailable."))
    elif not _git_exists(repo_root, ["merge-base", "--is-ancestor", record["source_commit"], "HEAD"]):
        findings.append(_finding("source_not_ancestor", "Release record source_commit is not an ancestor of the candidate tree."))
    else:
        changed = run_git(repo_root, ["diff", "--name-only", "-z", record["source_commit"],
                                      "HEAD", "--"], capture_output=True, check=False)
        if changed.returncode:
            findings.append(_finding("source_diff_failure", "Could not inspect changes after release record source."))
        else:
            for path in sorted(set(changed.stdout.decode("utf-8", errors="surrogateescape").split("\0")) - {""}):
                if not _post_source_document(path) and not _version_only_sync(
                    repo_root, record["source_commit"], path, record["version"]
                ):
                    findings.append(_finding("post_source_change", "Executable or configuration changed after release record source.", path))
    if not _git_exists(repo_root, ["rev-parse", "--verify", f"{record['baseline_tag']}^{{commit}}"]):
        findings.append(_finding("missing_baseline_tag", "Release record baseline_tag is unavailable."))
    elif not _git_exists(repo_root, ["merge-base", "--is-ancestor", record["baseline_tag"], record["source_commit"]]):
        findings.append(_finding("baseline_not_ancestor", "Release record baseline_tag is not an ancestor of source_commit."))
    for highlight in record["highlights"]:
        for evidence in highlight["evidence"]:
            if not _source_evidence_exists(repo_root, record, evidence):
                findings.append(_finding("missing_evidence", f"Release evidence is unavailable: {evidence}"))
    try:
        outputs = render_outputs(repo_root, record)
    except (OSError, ValueError) as exc:
        findings.append(_finding("render_failure", str(exc)))
        return {"ok": False, "findings": findings}
    for path, expected in outputs.items():
        candidate = repo_root / path
        actual = candidate.read_text(encoding="utf-8") if candidate.is_file() else ""
        if path.endswith(".md") and actual != expected:
            findings.append(_finding("rendered_output_mismatch", "Document does not match the deterministic release output.", path))
        if record["version"] not in expected:
            findings.append(_finding("missing_release_version", "Rendered document omits target version.", path))
    if target_version is None:
        for path in ("README.md", "ls/README.md"):
            text = (repo_root / path).read_text(encoding="utf-8")
            match = re.search(r"(?m)^\*\*Version:\*\* ([0-9]+\.[0-9]+\.[0-9]+)<br>$", text)
            if not match or match.group(1) != record["version"]:
                findings.append(_finding("readme_version_mismatch", "README version does not match the release record.", path))
    guide = outputs[f"ls/docs/releases/{record['version']}.md"]
    artifact_versions = re.findall(r"localsetup-v([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz", "\n".join(record["verification"]))
    if any(version != record["version"] for version in artifact_versions):
        findings.append(_finding("artifact_version_mismatch", "Verification instructions name a different framework release archive."))
    for sidecar in (".sha256", ".cdx.json"):
        if sidecar not in guide or sidecar not in "\n".join(record["verification"]):
            findings.append(_finding("missing_sidecar_instruction", f"Verification guidance must mention {sidecar}."))
    findings.extend(_check_links(repo_root, outputs))
    return {"ok": not findings, "findings": findings, "outputs": sorted(outputs)}
