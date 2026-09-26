from __future__ import annotations

from ls.tests.test_install_flow import *

def test_path_layout_validation_edge_cases(tmp_path: Path) -> None:
    from ls.core import paths

    for value, message in [
        ("", "must not be empty"),
        ("bad\x00path", "contains a NUL byte"),
        ("C:/Users/demo", "absolute Windows path"),
        ("../escape", "parent path"),
        ("/absolute", "repo-relative"),
        ("~/absolute", "repo-relative"),
    ]:
        with pytest.raises(paths.PathValidationError, match=message):
            paths.validate_repo_relative_path(value)

    with pytest.raises(paths.PathValidationError, match="scoped under the user home"):
        paths.validate_home_scoped_path("relative/path")
    assert str(paths.expand_user_path("~/demo")).endswith("/demo")

    with pytest.raises(paths.PathValidationError, match="must contain ls"):
        paths.source_layout(tmp_path / "not-source")

    source = tmp_path / "source"
    (source / "ls").mkdir(parents=True)
    assert paths.source_layout(source).source_root == source.resolve()
    layout = paths.global_layout(tmp_path / "home", package_root="~/pkg", registry_path="~/registry.json")
    assert layout.package_root == (tmp_path / "home" / "pkg").resolve()
    target = paths.target_layout(tmp_path / "target")
    assert paths.target_lockfile_path(target.target_root).name == "lock.json"
    assert paths.legacy_target_lockfile_path(target.target_root).name == "localsetup.lock.json"
    assert paths.target_journal_root(target.target_root).name == "install-journal"
    assert paths.target_backup_root(target.target_root).name == "backups"


def test_package_helpers_cover_error_and_mismatch_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core import package as pkg

    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("7.8.9\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\ndependencies = [\"plain-package>=1\", \"locked==2.0\"]\n",
        encoding="utf-8",
    )
    lock_text = """
version = 1

[[package]]
name = "localsetup"
version = "1.0.0"
source = { editable = "." }

[[package]]
name = "plain-package"
version = "1.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "locked"
version = "2.0"
source = { registry = "https://pypi.org/simple" }
"""
    (root / "uv.lock").write_text(lock_text, encoding="utf-8")
    artifact = tmp_path / "artifact.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        info = tarfile.TarInfo("uv.lock")
        data = lock_text.encode("utf-8")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    assert pkg._components_for_sbom(root)[0]["name"] == "locked"
    assert pkg._expected_components_from_artifact(artifact)
    empty_artifact = tmp_path / "empty.tar.gz"
    with tarfile.open(empty_artifact, "w:gz"):
        pass
    assert pkg._expected_components_from_artifact(empty_artifact) == []

    output = tmp_path / "source.cdx.json"
    fake_pack = SimpleNamespace(pack_id="pack", version=1, lockfile=".localsetup/lock.json")
    monkeypatch.setattr(pkg, "load_pack_config", lambda repo: fake_pack)
    monkeypatch.setattr("ls.core.manifests.load_pack_config", lambda repo: fake_pack)
    assert pkg.write_source_sbom(root, output)["component_count"] == 2
    source_sbom = json.loads(output.read_text(encoding="utf-8"))
    assert source_sbom["metadata"]["component"]["version"] == "7.8.9"

    target = tmp_path / "target"
    (target / ".localsetup").mkdir(parents=True)
    (target / ".localsetup" / "lock.json").write_text(
        json.dumps({"installed_skills": [str(tmp_path / "ls-a")], "installed_workflows": [str(tmp_path / "wf")]}),
        encoding="utf-8",
    )
    installed_output = tmp_path / "installed.cdx.json"
    assert pkg.write_installed_sbom(root, target, installed_output)["component_count"] == 2
    installed_sbom = json.loads(installed_output.read_text(encoding="utf-8"))
    assert installed_sbom["metadata"]["component"]["version"] == "7.8.9"

    missing_meta = tmp_path / "missing-meta.tar.gz"
    with tarfile.open(missing_meta, "w:gz"):
        pass
    with pytest.raises(ValueError, match="artifact metadata not found"):
        pkg.read_artifact_metadata(missing_meta)

    empty_sha = tmp_path / "empty.sha256"
    empty_sha.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty sha256"):
        pkg.parse_sha256_file(empty_sha)
    bad_sha = tmp_path / "bad.sha256"
    bad_sha.write_text("not-a-digest artifact.tar.gz\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid sha256 digest"):
        pkg.parse_sha256_file(bad_sha)

    assert pkg.verify_cyclonedx_sbom(tmp_path / "missing.cdx.json", artifact, {})["ok"] is False
    invalid_sbom = tmp_path / "invalid.cdx.json"
    invalid_sbom.write_text("{", encoding="utf-8")
    assert "invalid SBOM JSON" in pkg.verify_cyclonedx_sbom(invalid_sbom, artifact, {})["error"]

    with pytest.raises(ValueError, match="artifact not found"):
        pkg.verify_release_artifact(tmp_path / "missing.tar.gz")
    with pytest.raises(ValueError, match="sha256 file not found"):
        pkg.verify_release_artifact(artifact)

    metadata = {"schema_version": 1, "artifact": "other.tar.gz", "pack_id": "pack", "version": 1, "source_commit": "abc"}
    with tarfile.open(artifact, "w:gz") as tar:
        meta_bytes = json.dumps(metadata).encode("utf-8")
        meta = tarfile.TarInfo(pkg.ARTIFACT_METADATA_PATH)
        meta.size = len(meta_bytes)
        tar.addfile(meta, io.BytesIO(meta_bytes))
    digest = pkg.sha256_file(artifact)
    sha = artifact.with_name(f"{artifact.name}.sha256")
    sha.write_text(f"{digest}  wrong-name.tar.gz\n", encoding="utf-8")
    sbom = artifact.with_name(f"{artifact.name}.cdx.json")
    sbom.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "metadata": {
                    "component": {"name": "wrong"},
                    "properties": [
                        {"name": "localsetup:artifact", "value": "wrong"},
                        {"name": "localsetup:source_commit", "value": "wrong"},
                    ],
                },
                "components": [],
            }
        ),
        encoding="utf-8",
    )
    result = pkg.verify_release_artifact(artifact, expected_commit="expected", expected_tag="v1")
    assert result["ok"] is False
    assert {check["name"] for check in result["checks"]} >= {
        "sha256_filename",
        "metadata_artifact",
        "source_commit",
        "source_tag",
        "sbom",
    }


def test_framework_version_for_repo_falls_back_to_installed_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ls.core import framework_version as version_module
    from ls.core import package as pkg

    monkeypatch.setattr(version_module, "_source_version_path", lambda: None)
    monkeypatch.setattr(version_module.metadata, "version", lambda name: "9.8.7" if name == "localsetup" else "")

    root = tmp_path / "site-packages"
    root.mkdir()
    assert pkg._framework_version_for_repo(root) == "9.8.7"

    (root / "VERSION").write_text("7.6.5\n", encoding="utf-8")
    assert pkg._framework_version_for_repo(root) == "7.6.5"


def test_versioning_pure_and_check_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core import versioning as ver

    with pytest.raises(ValueError, match="invalid semantic version"):
        ver.SemVer.parse("not-semver")
    with pytest.raises(ValueError, match="unknown bump type"):
        ver.SemVer(1, 2, 3).bump("weird")
    assert ver.classify_commit("Merge branch main") == "none"
    assert ver.classify_commit("Revert something") == "none"
    assert ver.classify_commit("oops") == "patch"
    assert ver.classify_commit_for_release(tmp_path, ver.CommitInfo("a", "Merge branch", "")) == "none"
    assert ver.classify_commit_for_release(tmp_path, ver.CommitInfo("b", "Revert thing", "")) == "none"
    assert ver.release_type_override("Release-Type: Minor") == "minor"
    assert ver.version_from_sync_commit(ver.VERSION_SYNC_PREFIX) is None
    assert ver.version_from_sync_commit(f"{ver.VERSION_SYNC_PREFIX} nope") is None
    remaining, canceled = ver.net_unreleased_commits(
        [
            ver.CommitInfo("1", "feat: add thing", ""),
            ver.CommitInfo("2", 'Revert "missing"', ""),
            ver.CommitInfo("3", 'Revert "feat: add thing"', ""),
        ]
    )
    assert [commit.sha for commit in remaining] == ["2"]
    assert canceled[0]["original_sha"] == "1"

    def fake_run_git(repo_root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args[0] == "log":
            return subprocess.CompletedProcess(args, 0, "bad-record\x1eabc\x1fsubject\x1fbody\x1e", "")
        if args[:2] == ["rev-parse", "--verify"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["symbolic-ref", "--quiet"]:
            return subprocess.CompletedProcess(args, 1, "", "")
        if args[:2] == ["remote", "show"]:
            return subprocess.CompletedProcess(args, 0, "  HEAD branch: main\n", "")
        if args[0] == "merge-base":
            return subprocess.CompletedProcess(args, 0, "merge-sha\n", "")
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "head-sha\n", "")

    monkeypatch.setattr(ver, "_run_git", fake_run_git)
    assert ver.list_commits(tmp_path, "base", "base") == []
    assert len(ver.list_commits(tmp_path, "base", "head")) == 1
    assert ver._symbolic_remote_head(tmp_path, "origin") == "origin/main"
    with pytest.raises(ValueError, match="explicit base ref did not resolve"):
        ver.resolve_base_with_metadata(tmp_path, base="missing", head="head")

    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / "ls" / "docs").mkdir(parents=True)
    monkeypatch.setattr(ver, "_git_text", lambda *args, **kwargs: "")

    def sync_creates_file(repo_root: Path, target_version: str) -> dict:
        (repo_root / "pyproject.toml").write_text(target_version, encoding="utf-8")
        (repo_root / "VERSION").write_text(target_version, encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(ver, "sync_version_files", sync_creates_file)
    check = ver.check_version_files(tmp_path, "2.0.0")
    assert check["ok"] is True
    assert not (tmp_path / "pyproject.toml").exists()
    assert (tmp_path / "VERSION").read_text(encoding="utf-8") == "1.2.3\n"

    monkeypatch.setattr(ver, "plan_version", lambda *args, **kwargs: {"ok": True})
    plans = ver.push_lines_to_plans(
        tmp_path,
        "\ninvalid line\nrefs/heads/main " + ver.ZERO_SHA + " refs/heads/main abc\nrefs/heads/main local refs/heads/main remote\n",
    )
    assert plans == [{"ok": True}]

    monkeypatch.setattr(ver, "stage_version_files", lambda repo_root: None)
    monkeypatch.setattr(ver, "_git_text", lambda repo_root, args: "" if args[:2] == ["diff", "--cached"] else "head-sha")
    assert ver.commit_version_sync(tmp_path, "2.0.0") is None


def test_provenance_edge_cases_and_report_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ls.core import provenance as prov

    root = tmp_path / "repo"
    root.mkdir()
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")

    responses = {
        ("rev-parse", "HEAD^{tree}"): subprocess.CompletedProcess([], 0, "tree-sha\n", ""),
        ("status", "--porcelain", "--untracked-files=all"): subprocess.CompletedProcess(
            [],
            0,
            " M ls/docs/_generated/facts.json\nR  assets/README.md -> ls/docs/SKILLS.md\n",
            "",
        ),
        ("log", "-1", "--pretty=%s"): subprocess.CompletedProcess([], 0, "docs: refresh generated artifacts\n", ""),
        ("log", "-1", "--pretty=%s", "HEAD"): subprocess.CompletedProcess(
            [], 0, "docs: refresh generated artifacts\n", ""
        ),
        ("log", "-1", "--pretty=%s", "parent-sha"): subprocess.CompletedProcess([], 0, "fix: source\n", ""),
        ("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"): subprocess.CompletedProcess(
            [], 0, "ls/docs/_generated/facts.json\n", ""
        ),
        ("rev-parse", "HEAD^"): subprocess.CompletedProcess([], 0, "parent-sha\n", ""),
        ("describe", "--tags", "--exact-match", "parent-sha"): subprocess.CompletedProcess([], 1, "", ""),
        ("rev-parse", "parent-sha^{tree}"): subprocess.CompletedProcess([], 0, "parent-tree\n", ""),
        ("config", "--get", "remote.origin.url"): subprocess.CompletedProcess(
            [], 0, "git@github.com:CruxExperts/localsetup.git\n", ""
        ),
    }

    def fake_run_git(repo_root: Path, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return responses.get(tuple(args), subprocess.CompletedProcess(args, 1, "", "fail"))

    monkeypatch.setattr(prov, "run_git", fake_run_git)
    monkeypatch.setattr(prov, "source_commit", lambda repo: "head-sha")
    monkeypatch.setattr(prov, "source_tag", lambda repo: "v1")

    assert prov._status_entry_paths("??") == []
    assert prov._status_entry_paths("R  old -> new") == ["old", "new"]
    assert prov.source_dirty(root) is False
    assert prov.generated_artifact_parent_source_commit(root) == "parent-sha"
    base = prov.base_provenance(root, emitter="docs", generated_at=True, generated_commit_parent=True)
    assert base["source_commit"] == "parent-sha"
    assert base["source_tree_sha"] == "parent-tree"
    assert base["source_remote_url"] if "source_remote_url" in base else True
    assert prov.source_remote_url(root) == "https://github.com/CruxExperts/localsetup"
    assert prov.framework_version(tmp_path / "missing") == "unknown"

    package = tmp_path / "package"
    package.mkdir()
    (package / prov.MARKER_LEGACY).write_text("legacy marker\n", encoding="utf-8")
    assert prov.has_legacy_marker(package) is True
    assert prov.load_package_marker(package)["legacy_marker"] is True
    assert prov.marker_public_snapshot(None) is None

    rendered = prov.markdown_with_provenance(
        "---\ntitle: Old\nlocalsetup_provenance:\n  old: value\nframework_version: old\nsource_commit: old\nartifact_sha256: old\n---\n\nBody\n",
        base,
    )
    assert "title: Old" in rendered
    assert "old: value" not in rendered
    assert rendered.endswith("Body\n")
    assert "provenance" in prov.json_with_provenance({"a": 1}, base)

    content_path = root / "artifact.txt"
    content_path.write_text("artifact\n", encoding="utf-8")
    entry = prov.artifact_registry_entry(root, content_path, artifact_type="text", emitter="test")
    assert entry["path"] == "artifact.txt"

    global_root = tmp_path / "global"
    global_root.mkdir()
    stale = global_root / "ls-stale"
    stale.mkdir()
    (stale / "file.txt").write_text("current\n", encoding="utf-8")
    markerless = global_root / "ls-markerless"
    markerless.mkdir()
    legacy = global_root / "ls-legacy"
    legacy.mkdir()
    (legacy / prov.MARKER_LEGACY).write_text("legacy\n", encoding="utf-8")
    portable_global = global_root / "ls-portable"
    portable_global.mkdir()
    (portable_global / "SKILL.md").write_text("global\n", encoding="utf-8")

    portable_adapter = tmp_path / "portable-adapter"
    portable_pkg = portable_adapter / "ls-portable"
    portable_pkg.mkdir(parents=True)
    (portable_pkg / "SKILL.md").write_text("local drift\n", encoding="utf-8")
    registry_dir = root / "ls" / "docs" / "_generated"
    registry_dir.mkdir(parents=True)
    (registry_dir / "artifact-registry.json").write_text(
        json.dumps({"artifacts": [{"path": "missing.txt"}, {"path": "artifact.txt", "artifact_sha256": "wrong"}]}),
        encoding="utf-8",
    )

    report = prov.provenance_report(
        root,
        lock={
            "package_provenance": {
                "ls-stale": {"package_digest": "old"},
                "ls-missing": {"package_digest": "old"},
            }
        },
        registry={"packages": {"ls-stale": {"digest": "registry-old"}}},
        global_root=global_root,
        adapters=[
            {"repo_path": str(tmp_path / "global-adapter"), "points_to_global": True},
            {
                "repo_path": str(portable_adapter),
                "is_portable_copy": True,
                "package_integrity_failures": [],
            },
            {
                "repo_path": str(tmp_path / "scoped"),
                "is_scoped_symlink_adapter": True,
                "visible_packages": ["ls-context"],
                "expected_packages": ["ls-context", "ls-extra"],
                "package_integrity_failures": [{"package": None, "reason": "bad marker"}],
            },
            {"repo_path": str(tmp_path / "unmanaged"), "exists": True, "package_integrity_failures": []},
        ],
    )

    joined = "\n".join(report["warnings"])
    assert "target lock references stale package digest" in joined
    assert "target lock references missing global package digest" in joined
    assert "global registry digest differs" in joined
    assert "legacy plain managed marker" in joined
    assert "managed package marker missing" in joined
    assert "portable adapter package differs" in joined
    assert "scoped adapter package set differs" in joined
    assert "generated artifact missing" in joined
    assert "generated artifact has stale content digest" in joined
