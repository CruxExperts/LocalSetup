"""Lock drift must not silently change an independently installed skill runtime."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core.s3_sdk import exports

ROOT = Path(__file__).resolve().parents[2]


def test_locked_exports_match_both_independent_skills():
    assert exports.refresh(ROOT, check=True) == []
    data = exports.export(ROOT)
    assert b"pytest==" not in data and b"localsetup==" not in data
    assert b"boto3==1.43.108" in data


def test_every_distribution_needs_its_own_hash(monkeypatch):
    data = exports.export(ROOT)
    first_block_end = data.index(b"botocore==")
    incomplete = b"boto3==1.43.108 \\\n" + data[first_block_end:]
    monkeypatch.setattr(exports.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=incomplete))
    with pytest.raises(ValueError, match="hashes"):
        exports.export(ROOT)


def test_detects_drift_without_repairing_or_touching_other_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "export", lambda root: b"locked bytes")
    exports.refresh(tmp_path, check=False)
    changed = tmp_path / "ls/skills/ls-garage/requirements-s3-sdk.txt"
    changed.write_bytes(b"drift")
    assert exports.refresh(tmp_path, check=True) == [str(changed.relative_to(tmp_path))]
    assert changed.read_bytes() == b"drift"


def test_symlink_rejection_precedes_every_write(tmp_path, monkeypatch):
    monkeypatch.setattr(exports, "export", lambda root: b"locked bytes")
    exports.refresh(tmp_path, check=False)
    first = tmp_path / "ls/skills/ls-backblaze/requirements-s3-sdk.txt"
    second = tmp_path / "ls/skills/ls-garage/requirements-s3-sdk.txt"
    first.write_bytes(b"keep")
    second.unlink()
    second.symlink_to(first)
    with pytest.raises(ValueError, match="symlinks"):
        exports.refresh(tmp_path, check=False)
    assert first.read_bytes() == b"keep"
