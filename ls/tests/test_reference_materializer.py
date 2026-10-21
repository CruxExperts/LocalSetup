from __future__ import annotations

import json
from pathlib import Path
import pytest

from ls.core.reference_materializer import REFERENCE_BUNDLE_PATH
from ls.core.reference_materializer import classify_reference
from ls.core.reference_materializer import materialize_package_artifact
from ls.core.reference_materializer import validate_materialized_package


def test_reference_classifier_covers_public_private_and_source_paths() -> None:
    assert classify_reference("ls/docs/QUICKSTART.md").category == "public_doc"
    assert classify_reference("ls/docs/_generated/plugin-packs.md").category == "generated_public_doc"
    assert classify_reference("ls/docs/local-context/private.md").category == "private_doc"
    assert classify_reference("ls/docs/audits/report.md").category == "private_doc"
    assert classify_reference("docs/private.md").category == "private_doc"
    assert classify_reference(".localsetup-maint/docs/plan.md").category == "private_maintenance"
    assert classify_reference("ls/config/pack.yaml").category == "public_source_file"
    assert classify_reference("ls/lib/internal.py").category == "source_only_metadata"
    assert classify_reference("../escape.md").category == "blocked_escape"
    assert classify_reference("ls/docs/../../.localsetup-maint/secret.md").category == "blocked_escape"


def test_materializer_omits_bytecode_and_preserves_package_assets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "ls/skills/ls-demo"
    source.mkdir(parents=True)
    schema = repo / "ls/config/reference-bundle.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_bytes((Path(__file__).resolve().parents[1] / "config" / schema.name).read_bytes())
    retained = {"SKILL.md", "scripts/helper.py", ".hidden.txt", "data/fixture.txt"}
    excluded = {"scripts/__pycache__/helper.cpython-312.pyc", "scripts/__pycache__/note.txt", "legacy.pyc", "legacy.pyo"}
    for name in retained | excluded:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    destination = tmp_path / "materialized"

    manifest = materialize_package_artifact(
        repo, source, destination, package_name="ls-demo", package_type="skill", emitter="test"
    )

    assert manifest["validation"]["ok"] is True
    assert all((destination / name).read_bytes() == (source / name).read_bytes() for name in retained)
    assert all(not (destination / name).exists() for name in excluded)
    assert not (destination / "scripts/__pycache__").exists()
    assert all((source / name).exists() for name in excluded)


def test_materializer_rewrites_markdown_refs_and_copies_public_doc_closure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "ls" / "docs"
    skill = repo / "ls" / "skills" / "ls-demo"
    docs.mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (docs / "QUICKSTART.md").write_text("# Quickstart\n\nSee [Registry](PLATFORM_REGISTRY.md).\n", encoding="utf-8")
    (docs / "PLATFORM_REGISTRY.md").write_text("# Registry\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read [ls/docs/QUICKSTART.md](ls/docs/QUICKSTART.md) and "
        "`../../docs/PLATFORM_REGISTRY.md`.\n\n"
        "```bash\ncat ls/docs/QUICKSTART.md\n```\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    output = tmp_path / "out" / "ls-demo"
    text = (output / "SKILL.md").read_text(encoding="utf-8")
    assert "references/localsetup/docs/QUICKSTART.md" in text
    assert "`references/localsetup/docs/PLATFORM_REGISTRY.md`" in text
    assert "cat references/localsetup/docs/QUICKSTART.md" in text
    assert (output / "references/localsetup/docs/QUICKSTART.md").is_file()
    assert (output / "references/localsetup/docs/PLATFORM_REGISTRY.md").is_file()
    assert (output / REFERENCE_BUNDLE_PATH).is_file()
    assert manifest["validation"]["ok"] is True
    assert validate_materialized_package(output, repo_root=repo)["ok"] is True


def test_materializer_rejects_rewritten_public_doc_when_source_doc_is_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read [ls/docs/MISSING.md](ls/docs/MISSING.md).\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rewrite target is missing"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=[],
            emitter="test",
        )

    output = tmp_path / "out" / "ls-demo"
    assert not (output / "references/localsetup/docs/MISSING.md").exists()
    validation = validate_materialized_package(output, repo_root=repo, check_digest=False)
    assert validation["ok"] is False
    assert any("rewrite target is missing" in issue for issue in validation["issues"])


def test_materializer_rejects_dangling_localsetup_absolute_paths(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    stale = tmp_path / "old" / "ls" / "tools" / "missing"
    (package / "SKILL.md").write_text(f"Run `{stale}`.\n", encoding="utf-8")

    validation = validate_materialized_package(package, repo_root=tmp_path)

    assert validation["ok"] is False
    assert any("dangling LocalSetup absolute path" in issue for issue in validation["issues"])


def test_materializer_rejects_forbidden_paths_in_python_files(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "helper.py").write_text('DOC = "ls/docs/TOOLING_POLICY.md"\n', encoding="utf-8")

    validation = validate_materialized_package(package, repo_root=tmp_path)

    assert validation["ok"] is False
    assert any("forbidden LocalSetup path reference in helper.py" in issue for issue in validation["issues"])


def test_validate_materialized_package_rejects_unrecorded_framework_source_paths(tmp_path: Path) -> None:
    package = tmp_path / "package"
    manifest = package / REFERENCE_BUNDLE_PATH
    manifest.parent.mkdir(parents=True)
    (package / "SKILL.md").write_text("Read `ls/core/apply.py`.\n", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "materializer_version": 1,
                "classifier_version": 1,
                "package_name": "ls-demo",
                "package_type": "skill",
                "source_path": "ls/skills/ls-demo",
                "source_commit": "unknown",
                "emitter": "test",
                "copied_refs": [],
                "rewrites": [],
                "excluded_refs": [],
                "source_only_metadata": [],
                "runtime_resolved": [],
                "validation": {"ok": True, "issues": []},
                "digest": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    validation = validate_materialized_package(package, check_digest=False)

    assert validation["ok"] is False
    assert any("forbidden LocalSetup path reference in SKILL.md" in issue for issue in validation["issues"])


def test_materializer_records_framework_source_paths_as_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\nRead `ls/core/apply.py`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    assert {"path": "ls/core/apply.py", "source": str(skill / "SKILL.md")} in manifest["source_only_metadata"]


def test_materializer_rejects_unrecorded_existing_source_absolute_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    tool = repo / "ls" / "tools" / "tmux_ops"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        f"Run `{tool.resolve(strict=False)} pick`.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unrecorded LocalSetup absolute path"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=[],
            emitter="test",
        )


def test_materializer_rejects_dangling_package_root_absolute_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = tmp_path / "package"
    home = tmp_path / "home with spaces"
    package_root = home / ".local" / "share" / "localsetup" / "packages"
    package.mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    (repo / "ls" / "config" / "pack.yaml").write_text(
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
packs: {}
public_private:
  public_paths: []
  private_paths: []
""",
        encoding="utf-8",
    )
    stale = package_root / "ls-missing" / "SKILL.md"
    (package / "SKILL.md").write_text(f"Open `{stale}`.\n", encoding="utf-8")

    validation = validate_materialized_package(package, repo_root=repo, home=home, runtime_package_root=package_root)

    assert validation["ok"] is False
    assert any("dangling LocalSetup absolute path" in issue for issue in validation["issues"])


def test_materializer_records_directory_like_doc_literals_as_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs" / "_generated").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Generated artifacts live under `ls/docs/_generated/`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    assert {"path": "ls/docs/_generated/", "source": str(skill / "SKILL.md")} in manifest["source_only_metadata"]


def test_materializer_rejects_doc_reference_with_embedded_parent_traversal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs").mkdir(parents=True)
    (repo / ".localsetup-maint").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / ".localsetup-maint" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read [Secret](ls/docs/../../.localsetup-maint/secret.md).\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe runtime doc reference"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=[],
            emitter="test",
        )

    output = tmp_path / "out" / "ls-demo"
    assert not (output / "references/localsetup/.localsetup-maint/secret.md").exists()


@pytest.mark.parametrize(
    "body",
    [
        "Read `ls/docs/../../.localsetup-maint/secret.md`.\n",
        "Read ls/docs/../../.localsetup-maint/secret.md before continuing.\n",
    ],
)
def test_materializer_rejects_inline_and_bare_doc_reference_traversals(tmp_path: Path, body: str) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs").mkdir(parents=True)
    (repo / ".localsetup-maint").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / ".localsetup-maint" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n" + body,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe runtime doc reference"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=[],
            emitter="test",
        )


def test_validate_materialized_package_rejects_manifest_reference_escapes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )
    output = tmp_path / "out" / "ls-demo"
    manifest_path = output / REFERENCE_BUNDLE_PATH
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["copied_refs"] = ["ls/docs/../config/pack.yaml"]
    manifest["rewrites"] = [
        {"file": "ls/skills/ls-demo/SKILL.md", "from": "ls/docs/../config/pack.yaml", "to": "references/localsetup/docs/../config/pack.yaml"}
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validation = validate_materialized_package(output, repo_root=repo, check_digest=False)

    assert validation["ok"] is False
    assert any("copied_ref is not a public doc" in issue for issue in validation["issues"])
    assert any("rewrite target escapes bundled docs" in issue for issue in validation["issues"])


def test_materializer_records_workflow_required_docs_as_source_only_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workflow = repo / "ls" / "workflows" / "ls-workflow-demo"
    (repo / "ls" / "config").mkdir(parents=True)
    workflow.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (workflow / "SKILL.md").write_text("---\nname: ls-workflow-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    (repo / "ls" / "docs").mkdir(parents=True)
    (repo / "ls" / "docs" / "QUICKSTART.md").write_text("# Quickstart\n", encoding="utf-8")
    (workflow / "workflow.yaml").write_text(
        "id: demo\nname: Demo\nsummary: Demo\nrequired_docs:\n  - ls/docs/QUICKSTART.md\n",
        encoding="utf-8",
    )

    materialize_package_artifact(
        repo,
        workflow,
        tmp_path / "out" / "ls-workflow-demo",
        package_name="ls-workflow-demo",
        package_type="workflow",
        private_paths=[],
        emitter="test",
    )

    manifest = json.loads(((tmp_path / "out" / "ls-workflow-demo") / REFERENCE_BUNDLE_PATH).read_text(encoding="utf-8"))
    assert manifest["source_only_metadata"] == [
        {"path": "ls/docs/QUICKSTART.md", "source": "workflow.yaml.required_docs"}
    ]
    assert "ls/docs/QUICKSTART.md" in (tmp_path / "out" / "ls-workflow-demo" / "workflow.yaml").read_text(encoding="utf-8")


def test_materializer_rewrites_localsetup_resolver_tokens(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs" / "ops").mkdir(parents=True)
    (repo / "ls" / "tools").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "ls" / "docs" / "ops" / "tmux-ops-managed.md").write_text("# Managed\n", encoding="utf-8")
    (repo / "ls" / "tools" / "tmux_ops").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read `localsetup://doc/ops/tmux-ops-managed.md`.\n"
        "Run `localsetup://tool/tmux_ops pick`.\n"
        "Open `localsetup://package/ls-localsetup-project-context/SKILL.md`.\n",
        encoding="utf-8",
    )

    home = tmp_path / "home with spaces"
    package_root = home / ".local" / "share" / "localsetup" / "packages"

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        home=home,
        runtime_package_root=package_root,
        emitter="test",
    )

    text = (tmp_path / "out" / "ls-demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "localsetup://" not in text
    assert "references/localsetup/docs/ops/tmux-ops-managed.md" in text
    assert (tmp_path / "out" / "ls-demo" / "references" / "localsetup" / "docs" / "ops" / "tmux-ops-managed.md").is_file()
    assert str(repo / "ls" / "tools" / "tmux_ops") in text
    assert str(package_root / "ls-localsetup-project-context" / "SKILL.md") in text
    assert sorted(manifest["runtime_resolved"]) == sorted(
        [
            str(repo / "ls" / "tools" / "tmux_ops"),
            str(package_root / "ls-localsetup-project-context" / "SKILL.md"),
        ]
    )
    assert validate_materialized_package(
        tmp_path / "out" / "ls-demo",
        repo_root=repo,
        home=home,
        runtime_package_root=package_root,
    )["ok"] is True


def test_materializer_normalizes_prefixed_doc_resolver_tokens(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs" / "ops").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "ls" / "docs" / "ops" / "file.md").write_text("# File\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read `localsetup://doc/ls/docs/ops/file.md`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    output = tmp_path / "out" / "ls-demo"
    assert (output / "references" / "localsetup" / "docs" / "ops" / "file.md").is_file()
    assert manifest["copied_refs"] == ["ls/docs/ops/file.md"]
    assert "references/localsetup/docs/ops/file.md" in (output / "SKILL.md").read_text(encoding="utf-8")


def test_materializer_rejects_configured_private_resolver_doc_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs" / "ops" / "private").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "ls" / "docs" / "ops" / "private" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\n"
        "Read `localsetup://doc/ops/private/secret.md`.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unresolved resolver token"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=["ls/docs/ops/private"],
            emitter="test",
        )

    output = tmp_path / "out" / "ls-demo"
    assert not (output / "references" / "localsetup" / "docs" / "ops" / "private" / "secret.md").exists()


def test_materializer_rejects_private_raw_doc_reference_without_source_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    skill = repo / "ls" / "skills" / "ls-demo"
    (repo / "ls" / "docs" / "private").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (repo / "ls" / "docs" / "private" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    (skill / "notes.txt").write_text("Read ls/docs/private/secret.md.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden LocalSetup path reference"):
        materialize_package_artifact(
            repo,
            skill,
            tmp_path / "out" / "ls-demo",
            package_name="ls-demo",
            package_type="skill",
            private_paths=["ls/docs/private"],
            emitter="test",
        )

    manifest = json.loads(((tmp_path / "out" / "ls-demo") / REFERENCE_BUNDLE_PATH).read_text(encoding="utf-8"))
    assert manifest["source_only_metadata"] == []


def test_materializer_omits_private_legacy_doc_refs_inside_copied_docs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "ls" / "docs"
    skill = repo / "ls" / "skills" / "ls-demo"
    (docs / "private").mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (docs / "private" / "secret.md").write_text("# Secret\n", encoding="utf-8")
    (docs / "QUICKSTART.md").write_text(
        "# Quickstart\n\n"
        "Read ls/docs/private/secret.md.\n"
        "Open [Secret](./ls/docs/private/secret.md).\n"
        "See `./ls/docs/private/secret.md`.\n\n"
        "```bash\ncat ./ls/docs/private/secret.md\n```\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\nRead `ls/docs/QUICKSTART.md`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=["ls/docs/private"],
        emitter="test",
    )

    output = tmp_path / "out" / "ls-demo"
    copied = (output / "references" / "localsetup" / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    assert "ls/docs/private/secret.md" not in copied
    assert "private/secret.md" not in copied
    assert copied.count("omitted-private-reference") == 4
    assert "ls/docs/private/secret.md" not in manifest["copied_refs"]
    assert any(item["path"] == "ls/docs/private/secret.md" for item in manifest["excluded_refs"])
    assert validate_materialized_package(output, repo_root=repo)["ok"] is True


def test_materializer_preserves_doc_anchor_without_recording_anchor_in_copied_refs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "ls" / "docs"
    skill = repo / "ls" / "skills" / "ls-demo"
    docs.mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (docs / "QUICKSTART.md").write_text("# Quickstart\n\nSee `../../docs/DETAIL.md#part`.\n", encoding="utf-8")
    (docs / "DETAIL.md").write_text("# Detail\n\n## Part\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\nRead `ls/docs/QUICKSTART.md`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    output = tmp_path / "out" / "ls-demo"
    bundled = output / "references" / "localsetup" / "docs"
    assert (bundled / "DETAIL.md").is_file()
    assert "references/localsetup/docs/DETAIL.md#part" in (bundled / "QUICKSTART.md").read_text(encoding="utf-8")
    assert "ls/docs/DETAIL.md" in manifest["copied_refs"]
    assert "ls/docs/DETAIL.md#part" not in manifest["copied_refs"]


def test_materializer_rewrites_legacy_tool_refs_in_copied_docs_with_exact_runtime_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "ls" / "docs"
    skill = repo / "ls" / "skills" / "ls-demo"
    tools = repo / "ls" / "tools"
    docs.mkdir(parents=True)
    tools.mkdir(parents=True)
    (repo / "ls" / "config").mkdir(parents=True)
    skill.mkdir(parents=True)
    (repo / "ls" / "config" / "reference-bundle.schema.json").write_text(
        (Path(__file__).resolve().parents[1] / "config" / "reference-bundle.schema.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tools / "tmux_ops").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tools / "localsetup.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (docs / "QUICKSTART.md").write_text(
        "# Quickstart\n\n"
        "Run `ls/tools/tmux_ops pick`.\n"
        "See `ls/tools/tmux_ops.` and `ls/tools/localsetup.py,`.\n",
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\nRead `ls/docs/QUICKSTART.md`.\n",
        encoding="utf-8",
    )

    manifest = materialize_package_artifact(
        repo,
        skill,
        tmp_path / "out" / "ls-demo",
        package_name="ls-demo",
        package_type="skill",
        private_paths=[],
        emitter="test",
    )

    tool_path = str(repo / "ls" / "tools" / "tmux_ops")
    python_tool_path = str(repo / "ls" / "tools" / "localsetup.py")
    output = tmp_path / "out" / "ls-demo"
    copied = (output / "references" / "localsetup" / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    assert f"`{tool_path} pick`" in copied
    assert f"`{tool_path}.`" in copied
    assert f"`{python_tool_path},`" in copied
    assert manifest["runtime_resolved"] == sorted([python_tool_path, tool_path])


def test_validate_materialized_package_rejects_unrelated_absolute_tool_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = tmp_path / "package"
    tool = repo / "ls" / "tools" / "tmux_ops"
    other_tool = repo / "ls" / "tools" / "tmux_ops_helper"
    tool.parent.mkdir(parents=True)
    package.mkdir()
    tool.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    other_tool.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (package / "SKILL.md").write_text(f"Run `{other_tool}`.\n", encoding="utf-8")
    (package / REFERENCE_BUNDLE_PATH).parent.mkdir(parents=True)
    (package / REFERENCE_BUNDLE_PATH).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "materializer_version": 1,
                "classifier_version": 1,
                "package_name": "ls-demo",
                "package_type": "skill",
                "source_path": "ls/skills/ls-demo",
                "source_commit": "unknown",
                "emitter": "test",
                "copied_refs": [],
                "rewrites": [],
                "excluded_refs": [],
                "source_only_metadata": [],
                "runtime_resolved": [str(tool)],
                "validation": {"ok": True, "issues": []},
                "digest": "0" * 64,
            }
        ),
        encoding="utf-8",
    )

    validation = validate_materialized_package(package, repo_root=repo, check_digest=False)

    assert validation["ok"] is False
    assert any(f"unrecorded LocalSetup absolute path in SKILL.md: {other_tool}" in issue for issue in validation["issues"])


def test_validate_materialized_package_rejects_unresolved_resolver_tokens(tmp_path: Path) -> None:
    package = tmp_path / "package"
    manifest = package / REFERENCE_BUNDLE_PATH
    manifest.parent.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: ls-demo\ndescription: Demo.\n---\n\nRun `localsetup://tool/tmux_ops pick`.\n",
        encoding="utf-8",
    )
    manifest.write_text('{"schema_version": 1, "digest": "0"}\n', encoding="utf-8")

    validation = validate_materialized_package(package, check_digest=False)

    assert validation["ok"] is False
    assert any("unresolved resolver token" in issue for issue in validation["issues"])


def test_materializer_rejects_private_workflow_required_docs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workflow = repo / "ls" / "workflows" / "ls-workflow-demo"
    (repo / "ls" / "config").mkdir(parents=True)
    workflow.mkdir(parents=True)
    (workflow / "SKILL.md").write_text("---\nname: ls-workflow-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    (workflow / "workflow.yaml").write_text(
        "id: demo\nname: Demo\nsummary: Demo\nrequired_docs:\n  - .localsetup-maint/docs/secret.md\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="workflow required_docs is not publishable"):
        materialize_package_artifact(
            repo,
            workflow,
            tmp_path / "out" / "ls-workflow-demo",
            package_name="ls-workflow-demo",
            package_type="workflow",
            private_paths=[],
            emitter="test",
        )

    assert not (tmp_path / "out" / "ls-workflow-demo" / "workflow.yaml").exists()


def test_validate_materialized_package_rejects_missing_manifest_fields(tmp_path: Path) -> None:
    package = tmp_path / "package"
    manifest = package / REFERENCE_BUNDLE_PATH
    manifest.parent.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    manifest.write_text('{"schema_version": 1, "digest": "0"}\n', encoding="utf-8")

    validation = validate_materialized_package(package)

    assert validation["ok"] is False
    assert any("reference bundle missing fields" in issue for issue in validation["issues"])


def test_validate_materialized_package_rejects_schema_invalid_manifest_shape(tmp_path: Path) -> None:
    package = tmp_path / "package"
    manifest = package / REFERENCE_BUNDLE_PATH
    manifest.parent.mkdir(parents=True)
    (package / "SKILL.md").write_text("---\nname: ls-demo\ndescription: Demo.\n---\n", encoding="utf-8")
    payload = {
        "schema_version": "1",
        "materializer_version": 1,
        "classifier_version": 1,
        "package_name": "ls-demo",
        "package_type": "invalid",
        "source_path": "ls/skills/ls-demo",
        "source_commit": "unknown",
        "emitter": "test",
        "copied_refs": "ls/docs/QUICKSTART.md",
        "rewrites": [],
        "excluded_refs": [],
        "source_only_metadata": [],
        "runtime_resolved": [],
        "validation": {"ok": True, "issues": []},
        "digest": "0" * 64,
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    validation = validate_materialized_package(package)

    assert validation["ok"] is False
    assert any("schema_version must be 1" in issue for issue in validation["issues"])
    assert any("package_type must be skill or workflow" in issue for issue in validation["issues"])
    assert any("copied_refs must be a string list" in issue for issue in validation["issues"])
