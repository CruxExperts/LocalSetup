---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-audit
date: 2026-05-10
---

# Codex Bootstrap-Pack Validation

## Command Results

| Command | Result | Evidence |
|---|---|---|
| `date '+%Y%m%d-%H%M%S %Y-%m-%d %H:%M:%S %Z %z'` | pass | `20260510-223606 2026-05-10 22:36:06 CDT -0500` |
| `git status --short` at start | pass | Initial worktree was clean |
| `codex --version` | pass | `codex-cli 0.130.0` |
| `codex debug --help` | pass | `prompt-input` and `models` debug commands are available |
| `codex debug prompt-input 'bootstrap-pack audit prompt-load probe'` | pass | Rendered model-visible prompt input JSON |
| `python3 ls/tools/generate_docs_artifacts.py --repo-root .` | pass | Regenerated skills, workflow registry, quick ref, facts, and workflow catalog |
| `python3 ls/tools/localsetup.py --source-root . generate-docs` | pass | Regenerated aliases, migration map, platform adapters, skill packs, workflow catalog, and implementation file map |
| `python3 ls/tools/localsetup.py --source-root . plan --packs bootstrap` | pass | Effective install plan includes 12 skills and 3 workflow packages |
| `./ls/tools/verify_context` | pass | Context verification complete |
| `./ls/tools/verify_rules` | pass | Rule verification complete |
| `python3 ls/tools/localsetup.py --source-root . validate-catalog` | pass | `{"ok": true, "issues": []}` |
| `python3 ls/tools/localsetup.py --source-root . scan-migration` | pass | Exit 0; reported 599 informational legacy-name references in ignored run ledgers, generated migration/alias surfaces, and lockfile |
| `python3 ls/skills/ls-framework-audit/scripts/run_framework_audit.py` | pass | `Errors: 0, Warnings: 0` |
| `git diff --check` | pass | No whitespace errors |
| `./ls/tests/automated_test.sh` | pass | `9 passed, 0 failed` |
| `python3 -m pytest ls/tests` | pass | `114 passed in 20.31s` |
| Inline schema/artifact parse checker | pass | Exact command recorded below; parsed `pack.yaml`, bootstrap metadata, generated JSON, global Codex config, and agent TOMLs |
| Inline bootstrap membership checker | pass | Exact command recorded below; `included_skills`, `effective_install_skills`, and `included_workflows` match repo selectors |
| Inline changed-markdown local link checker | pass | Exact command recorded below; new and changed local markdown links resolved |
| Final read-only reviewer | pass | Original audit reviewer found no blocking audit findings; publish-pass reviewer finding on effective install skills was resolved and validation rerun |

## Reproducible Checker Commands

```bash
python3 - <<'PY'
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

root = Path(".").resolve()
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from ls.core.skills import selected_skill_names
from ls.core.workflows import selected_workflow_names

required = [
    "ls/docs/audits/codex-bootstrap-pack/AUDIT_REPORT.md",
    "ls/docs/audits/codex-bootstrap-pack/FINDINGS.yaml",
    "ls/docs/audits/codex-bootstrap-pack/REMEDIATION_PLAN.md",
    "ls/docs/audits/codex-bootstrap-pack/REMEDIATION_TASKS.yaml",
    "ls/docs/audits/codex-bootstrap-pack/VALIDATION.md",
]
required += sorted(str(p.relative_to(root)) for p in (root / "ls/docs/audits/codex-bootstrap-pack/agent-reports").glob("*.md"))

missing = [p for p in required if not (root / p).is_file()]
if missing:
    raise SystemExit(f"missing required artifacts: {missing}")

for rel in [
    "ls/config/pack.yaml",
    "ls/docs/bootstrap-packs/codex-agent-team/metadata.yaml",
    "ls/docs/audits/codex-bootstrap-pack/FINDINGS.yaml",
    "ls/docs/audits/codex-bootstrap-pack/REMEDIATION_TASKS.yaml",
]:
    yaml.safe_load((root / rel).read_text(encoding="utf-8"))

for rel in [
    "ls/docs/_generated/workflow-catalog.json",
    "ls/docs/_generated/facts.json",
    "ls/docs/_generated/skill_aliases.json",
]:
    json.loads((root / rel).read_text(encoding="utf-8"))

for rel in [
    os.path.expanduser("~/.codex/config.toml"),
    *[str(p) for p in Path(os.path.expanduser("~/.codex/agents")).glob("*.toml")],
]:
    tomllib.loads(Path(rel).read_text(encoding="utf-8"))

pack = yaml.safe_load((root / "ls/config/pack.yaml").read_text(encoding="utf-8"))
metadata = yaml.safe_load((root / "ls/docs/bootstrap-packs/codex-agent-team/metadata.yaml").read_text(encoding="utf-8"))
if sorted(metadata["included_skills"]) != sorted(pack["packs"]["bootstrap"]):
    raise SystemExit("metadata included_skills does not match direct bootstrap pack membership")
if sorted(metadata["included_workflows"]) != sorted(pack["workflow_packs"]["bootstrap"]):
    raise SystemExit("metadata included_workflows does not match bootstrap workflow pack membership")
if sorted(metadata["effective_install_skills"]) != selected_skill_names(root, ["bootstrap"]):
    raise SystemExit("metadata effective_install_skills does not match selected_skill_names(repo, ['bootstrap'])")
if sorted(metadata["included_workflows"]) != selected_workflow_names(root, ["bootstrap"]):
    raise SystemExit("metadata included_workflows does not match selected_workflow_names(repo, ['bootstrap'])")

status = subprocess.check_output(["git", "status", "--short", "--untracked-files=all"], text=True)
changed_md = []
for line in status.splitlines():
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    if path.endswith(".md") and (root / path).is_file():
        changed_md.append(path)

pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
bad = []
for rel in changed_md:
    text = (root / rel).read_text(encoding="utf-8")
    base = (root / rel).parent
    for target in pattern.findall(text):
        target = target.strip()
        if (
            not target
            or target.startswith(("#", "http://", "https://", "mailto:", "file:"))
            or target.startswith("<")
        ):
            continue
        target_path = target.split("#", 1)[0]
        if not target_path:
            continue
        resolved = (base / target_path).resolve()
        if not resolved.exists():
            bad.append(f"{rel}: {target}")

if bad:
    raise SystemExit("broken local markdown links:\n" + "\n".join(bad))

print("REQUIRED_OK")
print("PARSE_OK")
print("BOOTSTRAP_MEMBERSHIP_OK")
print("LINK_OK")
PY
```

## Prompt-To-Artifact Checklist

| Requirement | Evidence | Status |
|---|---|---|
| Generate an explicit plan | Ledger `.codex/runs/20260510-223606-codex-bootstrap-pack-audit.md` and controller update | met |
| Use reviewer/auditor/explorer/research/legacy-inventory subagents | Five saved reports under `agent-reports/`, including final reviewer | met |
| Save all audit artifacts | Required top-level artifacts plus subagent reports exist under `ls/docs/audits/codex-bootstrap-pack/` | met |
| Implement only low-risk docs/prompt/index/metadata fixes | Changes are repo-local docs, generated docs, pack metadata, and Codex template pointer | met |
| Do not make destructive changes | No delete/rename/reset/chmod operations were used | met |
| Do not change external folders or global config | No writes outside `<repo-root>` | met |
| Audit prior global Codex bootstrap | `agent-reports/codex-bootstrap-auditor.md` | met |
| Verify current Codex CLI behavior | `agent-reports/codex-cli-researcher.md`, local `codex --version`, `codex debug` checks, official OpenAI docs URLs | met |
| Create reusable bootstrap-pack structure | `ls/docs/bootstrap-packs/INDEX.md`, `codex-agent-team/metadata.yaml`, `AUDIT_PROMPT.md`, and `pack.yaml` bootstrap pack | met |
| Target OpenAI Codex first while leaving room for later frameworks | `metadata.yaml` has `primary_platform: codex` and future platform list | met |
| Deterministic remediation plan | `REMEDIATION_PLAN.md` and `REMEDIATION_TASKS.yaml` | met |
| Safe legacy replacement workflow | `agent-reports/legacy-inventory.md`, remediation phase 3, tasks `REM-005` and `REM-006` | met |
| Documentation artifacts organized and indexed | `ls/docs/README.md`, `ls/docs/AGENTIC_DESIGN_INDEX.md`, generated pack map and file map | met |
| Bootstrap-pack index/metadata present | `ls/docs/bootstrap-packs/INDEX.md`, `ls/docs/bootstrap-packs/codex-agent-team/metadata.yaml` | met |
| Internally validated | Catalog, parse checks, bootstrap membership check, generated docs, diff check, changed-link check, framework audit, smoke tests, pytest, and final review passed | met |

## Known Limits

- No global Codex files were changed.
- No legacy trees were replaced.
- Custom-agent TOML schema remains partly source-inferred because no complete public schema page was found.
- Goal mode remains drift-prone based on current research.
