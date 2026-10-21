"""A successful key-create result must never conceal truncated secret delivery."""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_short_write_cannot_be_reported_as_complete_delivery(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
    native = importlib.import_module("ls_backblaze.native")
    output = tmp_path / "new-secret"
    fd = native._reserve_secret(str(output))
    actual_write = os.write
    def short_write(descriptor, payload):
        return actual_write(descriptor, payload[:max(1, len(payload) // 2)])
    monkeypatch.setattr(native.os, "write", short_write)
    try:
        native._deliver_secret(fd, "test-only-secret-value")
    except Exception as exc:
        assert type(exc).__name__ == "ToolError"
        assert exc.code == "secret_delivery_failed"
    else:
        assert output.read_text() == "test-only-secret-value\n"


def test_secret_creation_rejects_symlink_ancestor_before_creating_file(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
    native = importlib.import_module("ls_backblaze.native")
    actual = tmp_path / "actual"
    (actual / "nested").mkdir(parents=True)
    (tmp_path / "link").symlink_to(actual, target_is_directory=True)
    with pytest.raises(Exception) as captured:
        native._reserve_secret(str(tmp_path / "link/nested/new-secret"))
    assert type(captured.value).__name__ == "ToolError"
    assert not (actual / "nested/new-secret").exists()


def test_secret_reservation_syncs_parent_before_remote_creation(monkeypatch, tmp_path):
    import stat
    monkeypatch.syspath_prepend(str(ROOT / "ls/skills/ls-backblaze-b2-storage/scripts/lib"))
    native = importlib.import_module("ls_backblaze.native")
    calls = []
    real = native.os.fsync
    def sync(fd):
        calls.append(stat.S_ISDIR(native.os.fstat(fd).st_mode))
        real(fd)
    monkeypatch.setattr(native.os, "fsync", sync)
    fd = native._reserve_secret(str(tmp_path / "secret"))
    native.os.close(fd)
    assert calls == [False, True]
