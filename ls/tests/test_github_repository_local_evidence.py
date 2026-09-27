from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from ls.core.github_repo.local_evidence import collect_local_evidence, recheck_social_preview_asset


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Local Evidence Test")
    _git(root, "config", "user.email", "local-evidence@example.invalid")


def _commit_all(root: Path) -> None:
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "fixture")


def test_collects_bounded_structural_evidence_for_tracked_paths_only(tmp_path: Path) -> None:
    root = tmp_path / "local-evidence-repository"
    _repository(root)
    (root / ".github").mkdir()
    (root / "README.md").write_text(
        "# Project\n\n[![Build](https://example.invalid/badge.svg)](https://example.invalid/build)\n",
        encoding="utf-8",
    )
    (root / "install").write_text("#!/bin/sh\ntouch executed-sentinel\n", encoding="utf-8")
    (root / "CONTRIBUTING.md").write_text("contribution route contains private-marker\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("security route\n", encoding="utf-8")
    (root / ".github" / "dependabot.yml").write_text("version: 2\n# local-secret-marker\n", encoding="utf-8")
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    _commit_all(root)
    (root / "CHANGELOG.md").write_text("untracked-secret-marker\n", encoding="utf-8")
    index = root / ".git" / "index"
    index_before = (index.read_bytes(), index.stat().st_mtime_ns)

    result = collect_local_evidence(root)

    assert result["supported"] is True
    assert set(result["controls"]) == {
        "readme_presentation", "status_badges", "installation_route", "support_route",
        "contribution_route", "security_reporting_route", "changelog_version_signals",
        "dependabot_configuration", "community_files", "pages_site_metadata",
        "open_graph_metadata_inputs", "accessibility_inputs", "footer_attribution_inputs",
    }
    assert result["controls"]["readme_presentation"]["status"] == "observed"
    assert result["controls"]["readme_presentation"]["value"]["tracked_paths"] == ["README.md"]
    assert result["controls"]["status_badges"]["value"]["markdown_badge_reference_count"] == 1
    assert result["controls"]["installation_route"]["value"]["tracked_paths"] == ["install"]
    assert result["controls"]["support_route"]["value"]["present"] is False
    assert result["controls"]["contribution_route"]["value"]["present"] is True
    assert result["controls"]["security_reporting_route"]["value"]["present"] is True
    assert result["controls"]["dependabot_configuration"]["value"]["tracked_paths"] == [".github/dependabot.yml"]
    assert "CHANGELOG.md" not in result["file_hashes"]
    assert "VERSION" in result["file_hashes"]
    assert result["social_preview"]["validation"] == "not_requested"
    assert (index.read_bytes(), index.stat().st_mtime_ns) == index_before
    assert not (root / "executed-sentinel").exists()

    serialized = json.dumps(result, sort_keys=True)
    assert str(root) not in serialized
    for private_marker in ("private-marker", "local-secret-marker", "untracked-secret-marker", "touch executed-sentinel"):
        assert private_marker not in serialized
    assert result["controls"]["changelog_version_signals"]["capability"] == "structural_presence_and_sha256"


def test_social_preview_present_binds_relative_path_bytes_and_rechecks_drift(tmp_path: Path) -> None:
    root = tmp_path / "social-preview-repository"
    _repository(root)
    (root / "assets").mkdir()
    image = b"\x89PNG\r\n\x1a\n" + b"sample-image-payload"
    (root / "assets" / "preview.bin").write_bytes(image)

    result = collect_local_evidence(root, social_preview_action="present", social_preview_asset="assets/preview.bin")
    preview = result["social_preview"]

    assert result["supported"] is True
    assert preview == {
        "action": "present",
        "handoff": "upload_in_settings",
        "evidence_scope": "local_file_only",
        "validation": "valid",
        "reason": None,
        "relative_path": "assets/preview.bin",
        "sha256": hashlib.sha256(image).hexdigest(),
        "size_bytes": len(image),
        "format": "png",
    }
    assert result["file_hashes"]["assets/preview.bin"] == {
        "sha256": preview["sha256"],
        "size_bytes": len(image),
    }
    assert str(root) not in json.dumps(result)

    unchanged = recheck_social_preview_asset(
        root, preview["relative_path"], preview["sha256"],
        expected_size=preview["size_bytes"], expected_format=preview["format"],
    )
    assert unchanged["supported"] is True and unchanged["matches"] is True
    (root / "assets" / "preview.bin").write_bytes(b"GIF89a" + b"changed")
    changed = recheck_social_preview_asset(root, "assets/preview.bin", preview["sha256"])
    assert changed["supported"] is True
    assert changed["matches"] is False
    assert changed["reason"] == "asset_changed"


def test_social_preview_absent_action_is_a_removal_handoff_and_differs_from_omitted(tmp_path: Path) -> None:
    root = tmp_path / "social-preview-absent-repository"
    _repository(root)

    omitted = collect_local_evidence(root)["social_preview"]
    absent = collect_local_evidence(root, social_preview_action="absent")["social_preview"]

    assert omitted["action"] is None
    assert omitted["validation"] == "not_requested"
    assert absent["action"] == "absent"
    assert absent["validation"] == "valid"
    assert absent["handoff"] == "remove_in_settings"
    assert absent["evidence_scope"] == "ui_handoff_only"
    assert absent["relative_path"] is None


@pytest.mark.parametrize(
    ("signature", "expected_format"),
    [(b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"), (b"GIF89a", "gif")],
)
def test_social_preview_recognizes_supported_image_signatures(
    tmp_path: Path,
    signature: bytes,
    expected_format: str,
) -> None:
    root = tmp_path / f"{expected_format}-social-preview-repository"
    _repository(root)
    asset = root / "preview.data"
    asset.write_bytes(signature + b"fixture-image")

    result = collect_local_evidence(root, social_preview_action="present", social_preview_asset="preview.data")

    assert result["social_preview"]["validation"] == "valid"
    assert result["social_preview"]["format"] == expected_format


@pytest.mark.parametrize(
    ("relative_path", "reason"),
    [("../outside.png", "asset_path_invalid"), ("/tmp/private.png", "asset_path_invalid"), ("assets\\image.png", "asset_path_invalid")],
)
def test_social_preview_rejects_unsafe_relative_paths(tmp_path: Path, relative_path: str, reason: str) -> None:
    root = tmp_path / "unsafe-social-preview-repository"
    _repository(root)

    result = collect_local_evidence(root, social_preview_action="present", social_preview_asset=relative_path)

    assert result["social_preview"]["validation"] == "invalid"
    assert result["social_preview"]["reason"] == reason
    assert result["social_preview"]["relative_path"] is None
    assert str(root) not in json.dumps(result)


def test_social_preview_rejects_symlink_and_oversized_files(tmp_path: Path) -> None:
    root = tmp_path / "invalid-image-repository"
    _repository(root)
    (root / "assets").mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
    (root / "assets" / "linked.png").symlink_to(outside)
    (root / "assets" / "large.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1_000_000)

    linked = collect_local_evidence(root, social_preview_action="present", social_preview_asset="assets/linked.png")["social_preview"]
    large = collect_local_evidence(root, social_preview_action="present", social_preview_asset="assets/large.png")["social_preview"]

    assert linked["validation"] == "invalid"
    assert linked["reason"] == "symlink_not_allowed"
    assert large["validation"] == "invalid"
    assert large["reason"] == "file_size_limit_exceeded"


def test_social_preview_rejects_unknown_bytes_and_nested_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "nested-social-preview-repository"
    _repository(root)
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (outside / "preview.png").write_bytes(b"not an image")
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    (root / "not-image.bin").write_bytes(b"not an image")

    bad_magic = collect_local_evidence(root, social_preview_action="present", social_preview_asset="not-image.bin")["social_preview"]
    escaped = collect_local_evidence(root, social_preview_action="present", social_preview_asset="linked-dir/preview.png")["social_preview"]

    assert bad_magic["reason"] == "unsupported_image_format"
    assert escaped["reason"] == "symlink_not_allowed"


def test_local_evidence_safe_failures_omit_checkout_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing-private-root"
    non_git = tmp_path / "non-git-private-root"
    non_git.mkdir()

    missing_result = collect_local_evidence(missing)
    non_git_result = collect_local_evidence(non_git)

    assert missing_result["supported"] is False
    assert missing_result["reason"] == "checkout_missing"
    assert non_git_result["supported"] is False
    assert non_git_result["reason"] == "not_git_repository"
    assert str(tmp_path) not in json.dumps(missing_result)
    assert str(tmp_path) not in json.dumps(non_git_result)
