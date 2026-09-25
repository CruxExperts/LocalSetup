"""Committed-history release-document audit planning."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ls.core.git_subprocess import run_git
from ls.core.versioning import read_version, resolve_head, plan_version
from ls.core.versioning_models import SemVer
from ls.core.provenance_source import is_generated_output_path

from .inventory import tracked_documents


def _committed_text(root: Path, ref: str, path: str) -> str:
    result = run_git(root, ["show", f"{ref}:{path}"], text=True, capture_output=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def _matches_document(document: str, content: str, changed_path: str) -> bool:
    if document == changed_path:
        return True
    path = Path(changed_path)
    # Documentation hyperlinks, VERSION, and generic stems such as "agent"
    # are not semantic dependencies. Their drift has deterministic gates.
    if path.suffix == ".md" or not path.suffix:
        return False
    if "/" not in changed_path:
        return document.startswith("ls/docs/") and f"`{changed_path}`" in content
    module = changed_path[:-3].replace("/", ".") if path.suffix == ".py" else None
    framework_path = changed_path.removeprefix("ls/")
    return changed_path in content or framework_path in content or bool(module and module in content)


def _source_material(root: Path, base: str, head: str, paths: list[str]) -> list[dict[str, str]]:
    """Return complete committed diffs, one per changed source path, without truncation."""
    material: list[dict[str, str]] = []
    for path in paths:
        result = run_git(root, ["diff", "--no-ext-diff", "--unified=3", base, head, "--", path],
                         text=True, capture_output=True, check=True)
        material.append({"path": path, "diff": result.stdout})
    return material


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    result = run_git(root, ["diff", "--name-only", "--no-ext-diff", base, head], text=True, capture_output=True, check=True)
    return sorted(line for line in result.stdout.splitlines() if line)


def _version_metadata_only(before: str, after: str, baseline: str, current: str) -> bool:
    """Recognize only canonical frontmatter version synchronization, not prose."""
    def normalized(text: str, version: str) -> str:
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            return text
        header, body = text[4:].split("\n---\n", 1)
        minor = ".".join(version.split(".")[:2])
        header = re.sub(r"(?m)^version: (?:" + re.escape(version) + "|" + re.escape(minor) + r")$",
                        "version: RELEASE_METADATA", header)
        return "---\n" + header + "\n---\n" + body
    return bool(before and after and before != after and
                normalized(before, baseline) == normalized(after, current))


def _reference_material(root: Path, head: str, version: str, changed: list[str]) -> list[dict[str, str]]:
    """Keep established update guidance available even for code-only releases."""
    references = []
    for path in ("ls/docs/QUICKSTART.md", "ls/docs/ADAPTER_OWNERSHIP.md",
                 f"ls/docs/releases/{version}.json"):
        if path in changed:
            continue
        content = _committed_text(root, head, path)
        if content:
            references.append({"path": path, "content":
                "UNCHANGED OPERATIONAL REFERENCE: use for compatibility, update, and verification guidance; "
                "do not describe this reference as a new release change.\n\n" + content})
    return references


def _tag_for_commit(root: Path, commit: str) -> str | None:
    result = run_git(root, ["tag", "--points-at", commit], text=True, capture_output=True, check=True)
    tags = sorted(tag for tag in result.stdout.splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+", tag))
    return tags[-1] if tags else None


def _prior_release(root: Path, head: str, current: str) -> tuple[str, str] | None:
    result = run_git(root, ["tag", "--merged", head], text=True, capture_output=True, check=True)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for tag in result.stdout.splitlines():
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            continue
        parsed = SemVer.parse(tag[1:])
        current_parsed = SemVer.parse(current)
        if (parsed.major, parsed.minor, parsed.patch) < (current_parsed.major, current_parsed.minor, current_parsed.patch):
            candidates.append(((parsed.major, parsed.minor, parsed.patch), tag))
    if not candidates:
        return None
    _, tag = max(candidates)
    return resolve_head(root, tag), tag


def plan(root: Path, base: str | None = None, head: str = "HEAD", repair: bool = False) -> dict[str, Any]:
    """Plan documentation coverage from canonical version planning and committed Git data."""
    repo_root = Path(root)
    version_plan = plan_version(repo_root, base=base, head=head)
    source_head = resolve_head(repo_root, head)
    explicit_base = resolve_head(repo_root, base) if base else None
    current_version = str(read_version(repo_root, source_head))
    if repair:
        previous = _prior_release(repo_root, source_head, current_version)
        source_base = previous[0] if previous else (explicit_base or source_head)
        baseline_tag = (previous[1] if previous else _tag_for_commit(repo_root, source_base)) or f"v{current_version}"
        target_version = current_version
    else:
        published_anchor = version_plan.get("published_anchor")
        if (isinstance(published_anchor, dict)
                and isinstance(version_plan.get("line_reconciliation"), dict)):
            source_base = str(published_anchor["commit"])
            baseline_tag = str(published_anchor["tag"])
        else:
            source_base = explicit_base or str(version_plan["base"])
            baseline_tag = _tag_for_commit(repo_root, source_base) or str(version_plan.get("anchor", {}).get("tag") or f"v{version_plan['base_version']}")
        target_version = version_plan["target_version"]
    documents = tracked_documents(repo_root)
    all_changed = _changed_paths(repo_root, source_base, source_head)
    generated_paths = [path for path in all_changed if is_generated_output_path(path)]
    changed_paths = [path for path in all_changed if path not in generated_paths]
    contents = {document: _committed_text(repo_root, source_head, document) for document in documents}
    baseline_version = str(read_version(repo_root, source_base))
    metadata_paths = [path for path in changed_paths if path.endswith(".md") and
                      _version_metadata_only(_committed_text(repo_root, source_base, path),
                                             contents[path] if path in contents else _committed_text(repo_root, source_head, path),
                                             baseline_version, current_version)]
    changed_paths = [path for path in changed_paths if path not in metadata_paths]
    affected: dict[str, list[str]] = {}
    for changed_path in changed_paths:
        matches = [
            document for document in documents
            if _matches_document(document, contents[document], changed_path)
        ]
        if changed_path in documents and changed_path not in matches:
            matches.append(changed_path)
        affected[changed_path] = sorted(matches)
    findings: list[dict[str, str]] = []
    for changed_path, matches in affected.items():
        if not matches:
            findings.append({"code": "coverage_pending", "path": changed_path,
                             "message": "No active public document references this changed path; release-document review must map or explain it."})
    if not documents:
        findings.append({"code": "no_public_documents", "message": "No tracked active public documentation was found."})
    if not version_plan.get("repairable", True) or version_plan.get("release_type_required"):
        findings.append({"code": "invalid_version_plan", "message": "Canonical release history requires reconciliation before documentation preparation."})
    return {
        "ok": bool(documents) and not any(item["code"] == "invalid_version_plan" for item in findings),
        "target_version": target_version,
        "source_commit": source_head,
        "source_base": source_base,
        "baseline_version": baseline_version,
        "baseline_tag": baseline_tag,
        "changed_paths": changed_paths,
        "generated_paths": generated_paths,
        "version_metadata_paths": metadata_paths,
        "documents": documents,
        "affected_documents": affected,
        "source_material": _source_material(repo_root, source_base, source_head, changed_paths),
        "reference_material": _reference_material(repo_root, source_head, current_version, changed_paths),
        "reference_version": current_version,
        "logical_slices": version_plan.get("logical_slices", []),
        "findings": findings,
        "repair": repair,
    }
