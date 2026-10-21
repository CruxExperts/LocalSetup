"""Markdown report rendering for pr_review.py."""

import time
from collections import Counter


CAT_ICONS = {
    "SECURITY": "[FAIL]",
    "ERROR_HANDLING": "[WARNING]",
    "RISK": "[WARNING]",
    "STYLE": "[NOTE]",
    "TODO": "[NOTE]",
    "TYPING": "[NOTE]",
}

FILE_CAT_ICONS = {
    "go": "[GO]",
    "python": "[PY]",
    "frontend": "[FE]",
    "ci": "[CI]",
    "config": "[CFG]",
    "docs": "[DOC]",
    "docker": "[DOCKER]",
    "sql": "[SQL]",
    "other": "[OTHER]",
}


def compose_report(
    *,
    repo: str,
    pr_num: int,
    view: dict,
    commits: str,
    categories: dict[str, list[str]],
    findings: list[dict],
    test_cov: str,
    lint_results: str,
) -> str:
    title = view.get("title", "")
    author = (view.get("author") or {}).get("login", "")
    branch = view.get("headRefName", "")
    head_sha = view.get("headRefOid", "")[:8]
    additions = view.get("additions", 0)
    deletions = view.get("deletions", 0)
    body = view.get("body") or "_No description provided._"
    created = view.get("createdAt", "")

    counts = Counter(f["category"] for f in findings)
    findings_summary_lines = []
    for cat, count in sorted(counts.items()):
        icon = CAT_ICONS.get(cat, "[NOTE]")
        findings_summary_lines.append(f"{icon} {cat}: {count}")
    findings_summary = "\n".join(findings_summary_lines) if findings_summary_lines else "[OK] No issues found in diff analysis"

    changed_files_md = []
    for cat, flist in sorted(categories.items()):
        changed_files_md.append(f"### {FILE_CAT_ICONS.get(cat, '[F]')} {cat.title()} ({len(flist)} files)")
        for f in flist:
            changed_files_md.append(f"- `{f}`")
        changed_files_md.append("")
    changed_files_md = "\n".join(changed_files_md)

    findings_table = ""
    if findings:
        findings_table = "| File | Line | Category | Finding | Context |\n|------|------|----------|---------|--------|\n"
        for f in findings[:50]:
            ctx = (f["context"] or "").replace("|", "\\|")[:60]
            short_file = f["file"].split("/")[-1]
            findings_table += f"| `{short_file}` | {f['line']} | {f['category']} | {f['message']} | `{ctx}` |\n"

    sec = [x for x in findings if x["category"] == "SECURITY"]
    err = [x for x in findings if x["category"] in ("ERROR_HANDLING", "RISK")]
    sty = [x for x in findings if x["category"] in ("STYLE", "TODO", "TYPING")]
    if sec:
        summary_verdict = "[FAIL] **SECURITY CONCERNS** — Review security findings before merging."
    elif err:
        summary_verdict = "[WARNING] **NEEDS ATTENTION** — Error handling / risk items to review."
    elif sty:
        summary_verdict = "[NOTE] **MINOR STYLE NOTES** — Looks good overall, minor suggestions above."
    else:
        summary_verdict = "[OK] **LOOKS GOOD** — No automated issues found. Ready for human review."

    report = f"""# PR #{pr_num} Review: {title}

**Author:** {author}
**Branch:** `{branch}`
**HEAD:** `{head_sha}`
**Created:** {created}
**Changes:** +{additions} / -{deletions}
**Reviewed:** {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}

## Description

{body}

## Commits

```
{commits}
```

## Changed Files

{changed_files_md}

## Automated Analysis

### Diff Findings

{findings_summary}

{findings_table}

### Test Coverage

{test_cov}

"""
    if lint_results:
        report += "### Local Lint Results\n\n" + lint_results + "\n\n"
    else:
        report += "### Local Lint\n\n_Skipped (repo not checked out locally or linters not found)._\n\n"
    return report + f"## Summary\n\n{summary_verdict}\n\n---\n_Automated PR review • {time.strftime('%Y-%m-%d %H:%M')}_\n"
