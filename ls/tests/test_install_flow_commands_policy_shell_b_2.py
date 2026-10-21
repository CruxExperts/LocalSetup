from __future__ import annotations

from ls.tests.test_install_flow import *

def test_config_file_and_cli_precedence(tmp_path: Path) -> None:
    config_path = tmp_path / "install.json"
    config_path.write_text(
        """{
  "platforms": ["codex"],
  "preset": "suggested",
  "packs": ["dev"],
  "skills": ["ls-localsetup-project-context"],
  "skill_classes": ["operations"],
  "skill_tags": ["git"],
  "exclude_skills": ["ls-linux-patch-planning"],
  "global_packs": ["bootstrap"],
  "global_preset": "custom",
  "global_skills": ["ls-localsetup-project-context"],
  "global_skill_classes": ["quality"],
  "global_skill_tags": ["testing"],
  "global_exclude_skills": ["ls-localsetup-framework-audit"],
  "repo_packs": ["core"],
  "repo_preset": "core",
  "repo_skills": ["ls-write-run-tests"],
  "repo_skill_classes": ["development"],
  "repo_skill_tags": ["git"],
  "repo_exclude_skills": ["ls-linux-patch-planning"],
  "attach_mode": "portable",
  "target_directory": "/tmp/localsetup-target",
  "data_root": "/tmp/localsetup-data",
  "dependency_mode": "prompt-only",
  "migration_mode": "report-only",
  "output": {"json": true}
}
""",
        encoding="utf-8",
    )

    base = load_install_config(config_path)
    merged = merge_cli_config(
        base,
        packs=["core"],
        preset="custom",
        skills=["ls-write-run-tests"],
        skill_classes=["quality"],
        skill_tags=["testing"],
        exclude_skills=["ls-context-index-search"],
        global_packs=["dev"],
        global_preset="suggested",
        repo_packs=["ops"],
        repo_preset="custom",
        attach_mode="symlink",
        dependency_mode="managed-venv",
    )

    assert base.platforms == ["codex"]
    assert base.preset == "suggested"
    assert base.packs == ["dev"]
    assert base.skills == ["ls-localsetup-project-context"]
    assert base.skill_classes == ["operations"]
    assert base.skill_tags == ["git"]
    assert base.exclude_skills == ["ls-linux-patch-planning"]
    assert base.global_packs == ["bootstrap"]
    assert base.global_preset == "custom"
    assert base.global_skills == ["ls-localsetup-project-context"]
    assert base.global_skill_classes == ["quality"]
    assert base.global_skill_tags == ["testing"]
    assert base.global_exclude_skills == ["ls-localsetup-framework-audit"]
    assert base.repo_packs == ["core"]
    assert base.repo_preset == "core"
    assert base.repo_skills == ["ls-write-run-tests"]
    assert base.repo_skill_classes == ["development"]
    assert base.repo_skill_tags == ["git"]
    assert base.repo_exclude_skills == ["ls-linux-patch-planning"]
    assert base.attach_mode == "portable"
    assert base.target_directory == "/tmp/localsetup-target"
    assert base.data_root == "/tmp/localsetup-data"
    assert merged.packs == ["core"]
    assert merged.preset == "custom"
    assert merged.skills == ["ls-write-run-tests"]
    assert merged.skill_classes == ["quality"]
    assert merged.skill_tags == ["testing"]
    assert merged.exclude_skills == ["ls-context-index-search"]
    assert merged.global_packs == ["dev"]
    assert merged.global_preset == "suggested"
    assert merged.global_skills == ["ls-localsetup-project-context"]
    assert merged.global_skill_classes == ["quality"]
    assert merged.global_skill_tags == ["testing"]
    assert merged.global_exclude_skills == ["ls-localsetup-framework-audit"]
    assert merged.repo_packs == ["ops"]
    assert merged.repo_preset == "custom"
    assert merged.repo_skills == ["ls-write-run-tests"]
    assert merged.repo_skill_classes == ["development"]
    assert merged.repo_skill_tags == ["git"]
    assert merged.repo_exclude_skills == ["ls-linux-patch-planning"]
    assert merged.attach_mode == "symlink"
    assert merged.target_directory == "/tmp/localsetup-target"
    assert merged.data_root == "/tmp/localsetup-data"
    assert merged.dependency_mode == "uv-sync"


def test_config_file_accepts_normal_preset_values(tmp_path: Path) -> None:
    config_path = tmp_path / "install-normal.json"
    config_path.write_text(
        json.dumps(
            {
                "preset": "normal",
                "global_preset": "normal",
                "repo_preset": "normal",
            }
        ),
        encoding="utf-8",
    )

    config = load_install_config(config_path)

    assert config.preset == "normal"
    assert config.global_preset == "normal"
    assert config.repo_preset == "normal"
    schema = json.loads((Path(__file__).resolve().parents[2] / "ls" / "config" / "install.schema.json").read_text(encoding="utf-8"))
    assert "default" not in schema["properties"]["packs"]


def test_schema_validation_is_optional_without_jsonschema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "required": ["name"]}),
        encoding="utf-8",
    )
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("simulated missing jsonschema")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert validate_json_schema({}, schema, label="example", required=False) == []
    assert validate_json_schema({}, schema, label="example") == ["jsonschema is required to validate example"]


def test_manifest_loader_reports_invalid_shapes(tmp_path: Path) -> None:
    from ls.core.manifests import ManifestError, load_pack_config, load_platforms, validate_manifest_schemas

    root = tmp_path / "repo"
    config = root / "ls" / "config"
    config.mkdir(parents=True)

    assert "pack.yaml schema validation failed: missing manifest" in validate_manifest_schemas(root)[0]

    (config / "pack.yaml").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="manifest is not a mapping"):
        load_pack_config(root)

    valid_base = {
        "pack_id": "localsetup",
        "namespace": "ls",
        "packs": {},
        "workflow_packs": {},
    }
    (config / "pack.yaml").write_text(json.dumps({**valid_base, "extensions": []}), encoding="utf-8")
    with pytest.raises(ManifestError, match="extensions must be a mapping"):
        load_pack_config(root)

    (config / "pack.yaml").write_text(
        json.dumps({**valid_base, "extensions": {"skill_taxonomy": []}}),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="extensions.skill_taxonomy must be a mapping"):
        load_pack_config(root)

    (config / "pack.yaml").write_text(
        json.dumps({**valid_base, "extensions": {"skill_taxonomy": {"ls-localsetup-project-context": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="extensions.skill_taxonomy.ls-context must be a mapping"):
        load_pack_config(root)

    (config / "platforms.yaml").write_text(json.dumps({"platforms": {}}), encoding="utf-8")
    with pytest.raises(ManifestError, match="platforms must be a list"):
        load_platforms(root)
