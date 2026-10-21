from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ls.core.plugin_packs import build_codex_plugins
from ls.core.plugin_packs import load_plugin_pack_configs
from ls.core.plugin_packs import plan_plugin_packs
from ls.core.plugin_packs import plugin_pack_catalog_payload
from ls.core.plugin_packs import validate_codex_plugin_path
from ls.core.plugin_packs import validate_plugin_pack_manifest
from ls.core.reference_materializer import REFERENCE_BUNDLE_PATH


ROOT = Path(__file__).resolve().parents[2]


def _reference_bundle_digest(payload: dict) -> str:
    data = {key: value for key, value in payload.items() if key != "digest"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def test_plugin_pack_manifest_loads_and_resolves_source_packs() -> None:
    configs = load_plugin_pack_configs(ROOT)
    payload = plugin_pack_catalog_payload(ROOT)

    assert validate_plugin_pack_manifest(ROOT) == []
    assert {config.source_pack for config in configs} == {
        "bootstrap",
        "core",
        "dev",
        "frontend",
        "architecture",
        "ops",
        "integrations",
        "publishing",
        "harness",
        "skill-lifecycle",
        "growth-content",
        "specialized",
    }
    assert payload["count"] == len(configs) == 12
    assert any(item["id"] == "localsetup-bootstrap" and "ls-localsetup-project-context" in item["skills"] for item in payload["plugin_packs"])
    assert any(
        item["id"] == "localsetup-harness" and "ls-context-index-search" in item["skills"]
        for item in payload["plugin_packs"]
    )
    dev = next(item for item in payload["plugin_packs"] if item["id"] == "localsetup-dev")
    assert "ls-skill-discovery" in dev["skills"]
    assert "ls-workflow-skills-index-refresh" not in dev["workflows"]


def test_plugin_pack_manifest_rejects_unknown_source_pack(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "ls" / "config", root / "ls" / "config")
    manifest = root / "ls" / "config" / "plugin-packs.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("source_pack: bootstrap", "source_pack: missing-pack", 1),
        encoding="utf-8",
    )

    issues = validate_plugin_pack_manifest(root)

    assert any("unknown source pack" in issue for issue in issues)


def test_plugin_pack_manifest_rejects_unsupported_platform(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "ls" / "config", root / "ls" / "config")
    manifest = root / "ls" / "config" / "plugin-packs.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("      codex:", "      opencode:", 1),
        encoding="utf-8",
    )

    issues = validate_plugin_pack_manifest(root)

    assert any("unsupported platform" in issue for issue in issues)


def test_plugin_pack_manifest_rejects_unsafe_context_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "ls" / "config", root / "ls" / "config")
    manifest = root / "ls" / "config" / "plugin-packs.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("- ls/docs/TOOLING_POLICY.md", "- ../escape", 1),
        encoding="utf-8",
    )

    issues = validate_plugin_pack_manifest(root)

    assert any("context input invalid" in issue and "parent path" in issue for issue in issues)


def test_plugin_pack_manifest_rejects_private_context_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "ls" / "config", root / "ls" / "config")
    private = root / ".localsetup-maint" / "docs"
    private.mkdir(parents=True)
    (private / "notes.md").write_text("# private\n", encoding="utf-8")
    manifest = root / "ls" / "config" / "plugin-packs.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("- ls/docs/TOOLING_POLICY.md", "- .localsetup-maint/docs/notes.md", 1),
        encoding="utf-8",
    )

    issues = validate_plugin_pack_manifest(root)

    assert any("path is private maintenance state" in issue for issue in issues)


def test_codex_plugin_generator_emits_marketplace_manifest_and_context(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"

    result = build_codex_plugins(ROOT, output, ["bootstrap"])

    plugin_root = output / "plugins" / "localsetup-bootstrap"
    manifest = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((output / "marketplace.json").read_text(encoding="utf-8"))
    validation = validate_codex_plugin_path(output)
    plan = plan_plugin_packs(ROOT, ["bootstrap"])

    assert result["ok"] is True
    assert marketplace["plugins"] == [{"name": "localsetup-bootstrap", "path": "./plugins/localsetup-bootstrap"}]
    assert manifest["name"] == "localsetup-bootstrap"
    assert manifest["interface"] == "v1"
    assert "ls-localsetup-project-context" in manifest["skills"]
    assert "ls-localsetup-framework-audit" in manifest["skills"]
    assert "ls-workflow-audit-framework" not in manifest["skills"]
    assert "ls-plugin-bootstrap-context" in manifest["skills"]
    assert (plugin_root / "skills" / "ls-plugin-bootstrap-context" / "SKILL.md").is_file()
    assert (plugin_root / "skills" / "ls-localsetup-project-context" / REFERENCE_BUNDLE_PATH).is_file()
    assert (plugin_root / "skills" / "ls-plugin-bootstrap-context" / REFERENCE_BUNDLE_PATH).is_file()
    assert validation["ok"] is True
    assert plan["plugin_packs"][0]["context_skill"] == "ls-plugin-bootstrap-context"


def test_codex_plugin_generator_rejects_external_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "ls", root / "ls", symlinks=True)
    shutil.copy2(ROOT / "VERSION", root / "VERSION")
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    link = root / "ls" / "skills" / "ls-localsetup-project-context" / "external-link"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink resolves outside"):
        build_codex_plugins(root, tmp_path / "plugin-out", ["bootstrap"])


def test_codex_plugin_validator_rejects_unsafe_plugin_output(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    build_codex_plugins(ROOT, output, ["bootstrap"])
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n", encoding="utf-8")
    link = output / "plugins" / "localsetup-bootstrap" / "skills" / "ls-localsetup-project-context" / "external-link"
    link.symlink_to(outside)

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("symlink resolves outside plugin root" in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_private_paths_and_skill_traversal(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    build_codex_plugins(ROOT, output, ["bootstrap"])
    plugin_root = output / "plugins" / "localsetup-bootstrap"
    private = plugin_root / ".localsetup-maint"
    private.mkdir()
    (private / "notes.md").write_text("private\n", encoding="utf-8")
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"].append("../escape")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("private maintenance path" in issue for issue in validation["issues"])
    assert any("parent path" in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_schema_invalid_reference_bundle(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    build_codex_plugins(ROOT, output, ["bootstrap"])
    bundle = output / "plugins" / "localsetup-bootstrap" / "skills" / "ls-localsetup-project-context" / REFERENCE_BUNDLE_PATH
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["schema_version"] = "1"
    payload["package_type"] = "invalid"
    payload["copied_refs"] = "ls/docs/QUICKSTART.md"
    payload["digest"] = _reference_bundle_digest(payload)
    bundle.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("schema_version must be 1" in issue for issue in validation["issues"])
    assert any("package_type must be skill or workflow" in issue for issue in validation["issues"])
    assert any("copied_refs must be a string list" in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_reference_bundle_additional_properties(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    build_codex_plugins(ROOT, output, ["bootstrap"])
    bundle = output / "plugins" / "localsetup-bootstrap" / "skills" / "ls-localsetup-project-context" / REFERENCE_BUNDLE_PATH
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    payload["unexpected"] = "extra"
    payload["rewrites"].append({"file": "SKILL.md", "from": "a", "to": "b", "unexpected": "extra"})
    payload["excluded_refs"].append({"path": "x", "category": "private_doc", "source": "test", "unexpected": "extra"})
    payload["source_only_metadata"].append({"path": "x", "source": "test", "unexpected": "extra"})
    payload["validation"]["unexpected"] = "extra"
    payload["digest"] = _reference_bundle_digest(payload)
    bundle.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("unsupported fields: unexpected" in issue for issue in validation["issues"])
    assert any("rewrites entries contain unsupported fields: unexpected" in issue for issue in validation["issues"])
    assert any("excluded_refs entries contain unsupported fields: unexpected" in issue for issue in validation["issues"])
    assert any("source_only_metadata entries contain unsupported fields: unexpected" in issue for issue in validation["issues"])
    assert any("validation contains unsupported fields: unexpected" in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_malformed_marketplace(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    output.mkdir()
    (output / "marketplace.json").write_text("{bad json", encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("invalid marketplace JSON" in issue for issue in validation["issues"])


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "marketplace JSON must be an object"),
        ({"plugins": "bad"}, "marketplace plugins must be a list"),
        ({"plugins": ["bad"]}, "marketplace plugin entry must be an object"),
    ],
)
def test_codex_plugin_validator_rejects_schema_wrong_marketplace_json(
    tmp_path: Path,
    payload: object,
    expected: str,
) -> None:
    output = tmp_path / "plugin-out"
    output.mkdir()
    (output / "marketplace.json").write_text(json.dumps(payload), encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any(expected in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_schema_wrong_plugin_manifest(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    manifest = output / ".codex-plugin"
    manifest.mkdir(parents=True)
    (manifest / "plugin.json").write_text("[]", encoding="utf-8")

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("manifest must be an object" in issue for issue in validation["issues"])


def test_codex_plugin_validator_rejects_marketplace_traversal_and_duplicates(tmp_path: Path) -> None:
    output = tmp_path / "plugin-out"
    output.mkdir()
    (output / "marketplace.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "duplicate", "path": "./plugins/one"},
                    {"name": "duplicate", "path": "../escape"},
                    {"name": "other", "path": "./plugins/one"},
                ]
            }
        ),
        encoding="utf-8",
    )

    validation = validate_codex_plugin_path(output)

    assert validation["ok"] is False
    assert any("duplicate marketplace plugin name" in issue for issue in validation["issues"])
    assert any("parent path" in issue for issue in validation["issues"])
    assert any("duplicate marketplace plugin path" in issue for issue in validation["issues"])
