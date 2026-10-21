from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def make_workflow_validation_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    config = root / "ls" / "config"
    skills = root / "ls" / "skills" / "ls-localsetup-project-context"
    workflow = root / "ls" / "workflows" / "ls-workflow-demo"
    docs = root / "ls" / "docs"
    config.mkdir(parents=True)
    skills.mkdir(parents=True)
    workflow.mkdir(parents=True)
    docs.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: ls-localsetup-project-context\ndescription: Context.\n---\n",
        encoding="utf-8",
    )
    (docs / "README.md").write_text("# Docs\n", encoding="utf-8")
    (config / "pack.yaml").write_text(
        """
pack_id: localsetup
namespace: ls
version: 3
global:
  home: ~/.local/share/localsetup
  package_root: ~/.local/share/localsetup/packages
  registry: ~/.local/share/localsetup/registry.json
repo:
  lockfile: .localsetup/lock.json
packs:
  core:
    - ls-localsetup-project-context
workflow_packs:
  core:
    - ls-workflow-demo
public_private:
  public_paths: []
  private_paths: []
""",
        encoding="utf-8",
    )
    (workflow / "SKILL.md").write_text(
        "---\nname: ls-workflow-demo\ndescription: Demo workflow.\n---\n",
        encoding="utf-8",
    )
    (workflow / "workflow.yaml").write_text(
        """
workflow_id: demo
display_name: Demo
aliases: [demo flow]
invocation: Demo only.
required_skills: []
required_tools: []
required_docs:
  - ls/docs/README.md
gates: []
phases: []
validation: []
outputs:
  - Demo output
smoke:
  - id: docs
    check: ls/docs/README.md exists
migration: {}
""",
        encoding="utf-8",
    )
    return root
