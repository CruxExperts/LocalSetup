from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ls.core.aliases import collect_skill_aliases
from ls.core.sdk_payload.ownership import upstream_documents
from ls.core.skills import load_skill_catalog, skill_taxonomy_payload
from ls.core.workflows import load_workflow_catalog

from .assets import build_assets_readme_text, collect_asset_manifest
from .constants import ASSETS_README, LIFECYCLE_STATES, SCHEMA_VERSION
from .inventory import collect_inventory, collect_truth_map
from .io import _frontmatter, _line_for_offset, _markdown_files, _markdown_links, _read_text, _rel, _resolve_markdown_target
from .models import Finding

def audit(repo_root: Path) -> dict[str, Any]:
    truth = collect_truth_map(repo_root)
    facts = truth["generated_facts"]
    generated_skill_taxonomy = truth["generated_skill_taxonomy"]
    truths = truth["truths"]
    skill_count = truths["skill_count"]["value"]
    workflow_count = truths["workflow_count"]["value"]
    version = truths["version"]["value"]
    valid_owner_skills = {skill.name for skill in load_skill_catalog(repo_root)}
    skill_aliases = collect_skill_aliases(repo_root / "ls" / "skills")
    valid_owner_packages = {workflow.package for workflow in load_workflow_catalog(repo_root)} | {"generate-docs", "docs-align"}
    findings: list[Finding] = []

    if truths["version"]["pyproject_version"] and truths["version"]["pyproject_version"] != version:
        findings.append(Finding("version.pyproject", "critical", "version_drift", "pyproject.toml", None, "pyproject version does not match VERSION", version, truths["version"]["pyproject_version"], "VERSION", "generated"))
    if facts:
        for key, expected in (("version", version), ("skill_count", skill_count), ("workflow_count", workflow_count), ("platform_count", truths["platform_count"]["value"])):
            actual = facts.get(key)
            if actual != expected:
                findings.append(Finding(f"facts.{key}", "critical", "generated_drift", "ls/docs/_generated/facts.json", None, f"generated facts `{key}` is stale", expected, actual, "live manifests", "generated"))
    expected_skill_taxonomy = truths["skill_taxonomy"]["value"]
    if generated_skill_taxonomy:
        actual_skill_taxonomy = {k: v for k, v in generated_skill_taxonomy.items() if k != "provenance"}
        if actual_skill_taxonomy != expected_skill_taxonomy:
            findings.append(Finding("skill_taxonomy.generated", "critical", "generated_drift", "ls/docs/_generated/skill-taxonomy.json", None, "generated skill taxonomy is stale", expected_skill_taxonomy, actual_skill_taxonomy, "live skill taxonomy", "generated"))
    else:
        findings.append(Finding("skill_taxonomy.missing", "critical", "generated_drift", "ls/docs/_generated/skill-taxonomy.json", None, "generated skill taxonomy is missing", expected_skill_taxonomy, None, "live skill taxonomy", "generated"))

    count_re = re.compile(r"\b(\d+)\s+shipped(?:\s+capability)?\s+skills?\s+plus\s+(\d+)\s+(?:first-class\s+)?workflow\s+packages\b", re.IGNORECASE)
    upstream = upstream_documents(repo_root)
    for path in _markdown_files(repo_root):
        rel = _rel(repo_root, path)
        if rel in upstream:
            continue
        text = _read_text(path)
        for match in count_re.finditer(text):
            actual = {"skills": int(match.group(1)), "workflows": int(match.group(2))}
            expected = {"skills": skill_count, "workflows": workflow_count}
            if actual != expected:
                findings.append(Finding(f"count.{rel}:{_line_for_offset(text, match.start())}", "major", "stale_count", rel, _line_for_offset(text, match.start()), "hard-coded shipped skill/workflow count is stale", expected, actual, "live skill/workflow manifests", "public"))

        if text.count("```") % 2:
            findings.append(Finding(f"fence.{rel}", "major", "markdown", rel, None, "unclosed fenced code block", "even fence count", "odd fence count", rel, "public"))

        if rel.startswith("ls/docs/") and not rel.startswith("ls/docs/_generated/") and "/local-context/" not in rel:
            fm = _frontmatter(text)
            if fm.get("status") not in LIFECYCLE_STATES or not fm.get("version"):
                findings.append(Finding(f"lifecycle.{rel}", "major", "lifecycle", rel, 1, "framework doc is missing valid lifecycle status/version frontmatter", "status and version", fm, "docs.config.yaml", "public"))
            if fm.get("status") == "ACTIVE":
                owner_skill = str(fm.get("owner_skill", "")).strip()
                owner_package = str(fm.get("owner_package", "")).strip()
                if not owner_skill and not owner_package:
                    findings.append(Finding(f"ownership.{rel}", "major", "ownership", rel, 1, "active framework doc is missing owner_skill or owner_package frontmatter", "owner_skill or owner_package", fm, "skill-owned documentation policy", "public"))
                if owner_skill and skill_aliases.get(owner_skill, owner_skill) not in valid_owner_skills:
                    findings.append(Finding(f"ownership_skill.{rel}", "major", "ownership", rel, 1, "active framework doc references missing owner_skill", sorted(valid_owner_skills), owner_skill, "skill catalog", "public"))
                if owner_package and owner_package not in valid_owner_packages:
                    findings.append(Finding(f"ownership_package.{rel}", "major", "ownership", rel, 1, "active framework doc references missing owner_package", sorted(valid_owner_packages), owner_package, "workflow catalog and generated packages", "public"))

        source_snapshot = path.name.endswith(".source.md")
        for kind, target, offset, label in _markdown_links(text):
            line = _line_for_offset(text, offset)
            if kind == "image" and not label:
                findings.append(Finding(f"image_alt.{rel}:{line}", "minor", "asset", rel, line, "image is missing non-empty alt text", "non-empty alt text", label, "GitHub Markdown guidance", "public"))
            if source_snapshot:
                continue
            resolved = _resolve_markdown_target(repo_root, path, target)
            if resolved and not resolved.exists():
                findings.append(Finding(f"link.{rel}:{line}", "major", "link", rel, line, f"relative {kind} target does not exist: {target}", "existing path", target, rel, "public"))

    for rel in ("README.md", "ls/docs/README.md", "ls/docs/FEATURES.md"):
        path = repo_root / rel
        if path.exists() and "facts-block:start" not in _read_text(path):
            findings.append(Finding(f"managed_block.{rel}", "major", "generated_block", rel, None, "missing generated facts block", "facts-block", None, "ls/docs/_generated/facts.json", "generated"))

    assets_readme = repo_root / ASSETS_README
    expected_assets_readme = build_assets_readme_text(collect_asset_manifest(repo_root))
    actual_assets_readme = _read_text(assets_readme) if assets_readme.exists() else ""
    if actual_assets_readme != expected_assets_readme:
        findings.append(
            Finding(
                "asset_readme.drift",
                "major",
                "asset",
                str(ASSETS_README),
                None,
                "asset README is stale relative to the generated asset manifest",
                "current generated asset README",
                "missing or stale asset README",
                "assets/* and markdown image references",
                "assets",
            )
        )

    findings_dict = [finding.as_dict() for finding in findings]
    severity_rank = {"critical": 4, "major": 3, "minor": 2, "info": 1}
    findings_dict.sort(key=lambda row: (-severity_rank.get(str(row["severity"]), 0), row["path"], row["id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not any(row["severity"] in {"critical", "major"} for row in findings_dict),
        "finding_count": len(findings_dict),
        "critical_count": sum(1 for row in findings_dict if row["severity"] == "critical"),
        "major_count": sum(1 for row in findings_dict if row["severity"] == "major"),
        "minor_count": sum(1 for row in findings_dict if row["severity"] == "minor"),
        "findings": findings_dict,
    }


def build_plan(audit_result: dict[str, Any]) -> dict[str, Any]:
    buckets = {
        "generated_fixes": [],
        "public_doc_rewrites": [],
        "lifecycle_cleanup": [],
        "asset_cleanup": [],
        "ownership_cleanup": [],
        "ci_changes": [],
        "manual_review": [],
    }
    for finding in audit_result["findings"]:
        category = finding["category"]
        if finding["fix_scope"] == "generated":
            buckets["generated_fixes"].append(finding["id"])
        elif category == "stale_count":
            buckets["public_doc_rewrites"].append(finding["id"])
        elif category == "lifecycle":
            buckets["lifecycle_cleanup"].append(finding["id"])
        elif category == "asset":
            buckets["asset_cleanup"].append(finding["id"])
        elif category == "ownership":
            buckets["ownership_cleanup"].append(finding["id"])
        else:
            buckets["manual_review"].append(finding["id"])
    return {"schema_version": SCHEMA_VERSION, "ok": audit_result["ok"], "buckets": buckets}
