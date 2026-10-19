"""Behavioral checks for the optional standalone capture archive tool."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
from io import BytesIO

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/ls-skill-capture-handoff/scripts/package_skill.py"


def package_tool():
    spec = importlib.util.spec_from_file_location("capture_package_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capture_is_selected_only_with_opt_in_pack():
    from ls.core.selection import resolve_package_selection

    root = Path(__file__).resolve().parents[2]
    normal = resolve_package_selection(root, preset="normal", target_root=root)
    assert "ls-skill-capture-handoff" not in normal.skills
    selected = resolve_package_selection(root, packs=["skill-lifecycle"], target_root=root)
    assert "ls-skill-capture-handoff" in selected.skills


def test_archive_is_reproducible_and_manifest_verifies_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Useful skill\n")
    (source / "LICENSE").write_text("MIT\n")
    tool = package_tool()
    first = tool.archive_bytes(tool.reviewed_files(source, ["SKILL.md", "LICENSE"]))
    second = tool.archive_bytes(tool.reviewed_files(source, ["LICENSE", "SKILL.md"]))
    assert first == second
    with tarfile.open(fileobj=BytesIO(first), mode="r:gz") as archive:
        manifest = json.load(archive.extractfile("MANIFEST.json"))
        assert [entry["path"] for entry in manifest["files"]] == ["LICENSE", "SKILL.md"]
        for entry in manifest["files"]:
            data = archive.extractfile(entry["path"]).read()
            assert entry["bytes"] == len(data)
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()


def test_existing_archive_is_never_replaced(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Skill\n")
    paths = tmp_path / "paths.json"
    paths.write_text('["SKILL.md"]')
    output = tmp_path / "archive.tar.gz"
    output.write_bytes(b"original")
    tool = package_tool()
    monkeypatch.setattr(sys, "argv", ["package_skill.py", "--root", str(source),
                                   "--files", str(paths), "--output", str(output)])
    with pytest.raises(SystemExit) as stopped:
        tool.main()
    assert stopped.value.code == 1
    assert output.read_bytes() == b"original"
    assert "archive preparation failed" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["../secret.md", "/tmp/secret.md", ".agents/state/log.md",
                                  "state/log.md", "MANIFEST.json", "sub/../SKILL.md"])
def test_private_or_escaping_paths_are_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        package_tool().reviewed_files(tmp_path, [name])


def test_symlinked_source_is_rejected(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    root = tmp_path / "root"
    root.mkdir()
    (root / "SKILL.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlinks"):
        package_tool().reviewed_files(root, ["SKILL.md"])
