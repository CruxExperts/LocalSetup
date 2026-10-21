"""Herdr's installed suite and public domain entrypoint."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ls.core.reference_materializer import materialize_package_artifact
from ls.core.selection import resolve_package_selection


ROOT = Path(__file__).resolve().parents[2]
SUITE = {
    "ls-herdr-task-routing", "ls-herdr-pane-layout", "ls-herdr-tasks", "ls-herdr-remote",
    "ls-herdr-provenance-drift",
}


def test_normal_and_explicit_master_install_the_functional_suite() -> None:
    normal = resolve_package_selection(ROOT, preset="normal")
    master = resolve_package_selection(ROOT, preset="custom", skills=["ls-herdr-task-routing"])
    assert SUITE <= set(normal.skills)
    assert SUITE <= set(master.skills)


def test_materialized_master_preserves_immutable_source_evidence(tmp_path: Path) -> None:
    destination = tmp_path / "packages" / "ls-herdr-task-routing"
    result = materialize_package_artifact(
        ROOT, ROOT / "ls/skills/ls-herdr-task-routing", destination,
        package_name="ls-herdr-task-routing", package_type="skill", home=tmp_path / "home",
        runtime_package_root=tmp_path / "packages", emitter="herdr-installation-test",
    )
    assert result["validation"]["ok"], result["validation"]
    source = ROOT / "ls/skills/ls-herdr-task-routing/references/upstream"
    manifest = json.loads((source / "provenance.json").read_text())
    for row in manifest["upstream_files"]:
        output = destination / "references/upstream" / row["path"]
        assert hashlib.sha256(output.read_bytes()).hexdigest() == row["sha256"]
        assert output.read_bytes() == (source / row["path"]).read_bytes()
    assert "localsetup://" not in (destination / "SKILL.md").read_text()


@pytest.mark.parametrize("prefix", [[], ["--source-root", str(ROOT)], [f"--source-root={ROOT}"]])
def test_public_cli_forwards_domain_options_verbatim(prefix: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "ls/tools/localsetup.py"), *prefix,
         "herdr", "--json", "provenance", "check"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload.get("ok", True), payload


def test_domain_help_is_available_without_a_live_terminal() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "ls/tools/localsetup.py"), "herdr", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert all(group in result.stdout for group in ("pane", "task", "remote", "provenance"))
