from __future__ import annotations

from pathlib import Path
import os
import stat

import pytest

import ls.core.openpgp.generation_home as generation_home
from ls.core.openpgp.generation_home import (
    PreparedGenerationHome,
    check_generation_parent,
    check_generation_selection,
    inspect_generation_home,
)
from ls.core.openpgp.generation_models import (
    KeyGenerationError,
    KeyGenerationErrorCode,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_inspection_is_read_only_and_accepts_missing_or_permissive_empty_leaf(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-created-yet"
    missing_selection = inspect_generation_home(missing)
    assert missing_selection.path == missing
    assert missing_selection.initial_leaf is None
    assert not missing.exists()
    check_generation_selection(missing_selection)

    permissive = tmp_path / "permissive-empty"
    permissive.mkdir(mode=0o700)
    permissive.chmod(0o755)
    before = permissive.stat()
    selection = inspect_generation_home(permissive)
    after = permissive.stat()
    assert selection.initial_leaf is not None
    assert selection.initial_leaf.mode == 0o755
    assert (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_mode) == (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_mode,
    )
    check_generation_selection(selection)
    check_generation_parent(selection)


@pytest.mark.parametrize("replacement", ["appeared", "changed_mode", "replaced", "symlink"])
def test_selection_recheck_rejects_leaf_changes_without_mutating_them(
    tmp_path: Path,
    replacement: str,
) -> None:
    home = tmp_path / "selected-home"
    if replacement == "changed_mode":
        home.mkdir(mode=0o700)
        home.chmod(0o755)
    selection = inspect_generation_home(home)
    target = tmp_path / "external-target"
    target.mkdir(mode=0o700)
    target.chmod(0o755)
    marker = target / "leave-alone"
    marker.write_text("external", encoding="utf-8")
    target_before = (target.stat().st_ino, _mode(target), marker.read_bytes())

    if replacement == "appeared":
        home.mkdir(mode=0o700)
        (home / "external-entry").write_text("leave", encoding="utf-8")
    elif replacement == "changed_mode":
        home.chmod(0o750)
    elif replacement == "replaced":
        home.mkdir(mode=0o700)
        moved = tmp_path / "moved-home"
        home.rename(moved)
        home.mkdir(mode=0o700)
        (home / "replacement").write_text("leave", encoding="utf-8")
    else:
        home.symlink_to(target, target_is_directory=True)

    with pytest.raises(KeyGenerationError) as failure:
        check_generation_selection(selection)
    assert failure.value.code is KeyGenerationErrorCode.KEYRING_UNAVAILABLE
    assert (target.stat().st_ino, _mode(target), marker.read_bytes()) == target_before
    if replacement == "symlink":
        assert home.is_symlink()
    elif replacement == "changed_mode":
        assert _mode(home) == 0o750
    elif replacement in {"appeared", "replaced"}:
        assert (home / ("external-entry" if replacement == "appeared" else "replacement")).exists()


def test_prepared_home_hardening_uses_original_open_directory_after_replacement(
    tmp_path: Path,
) -> None:
    home = tmp_path / "prepared-home"
    selection = inspect_generation_home(home)
    prepared = PreparedGenerationHome.prepare(selection)
    moved = tmp_path / "prepared-home-moved"
    replacement = tmp_path / "replacement-home"
    try:
        home.rename(moved)
        replacement.mkdir(mode=0o700)
        replacement.chmod(0o755)
        marker = replacement / "keep"
        marker.write_text("replacement", encoding="utf-8")

        with pytest.raises(KeyGenerationError) as stale:
            prepared.check_current()
        assert stale.value.code is KeyGenerationErrorCode.KEYRING_UNAVAILABLE

        prepared.harden()
        assert _mode(moved) == 0o700
        assert _mode(replacement) == 0o755
        assert marker.read_text(encoding="utf-8") == "replacement"
    finally:
        prepared.close()


def test_existing_permissive_leaf_is_hardened_only_after_acceptance_callback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "accepted-home"
    home.mkdir(mode=0o700)
    home.chmod(0o755)
    selection = inspect_generation_home(home)
    calls: list[str] = []

    def accepted() -> None:
        calls.append("accepted")

    prepared = PreparedGenerationHome.prepare(selection, before_mutation=accepted)
    try:
        assert calls == ["accepted"]
        assert _mode(home) == 0o700
        prepared.check_current()
    finally:
        prepared.close()


def test_prepare_failure_before_leaf_open_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "selected-home"
    selection = inspect_generation_home(home)
    moved = tmp_path / "created-original"
    real_open_directory_at = generation_home._open_directory_at

    def replace_before_open(parent_fd: int, name: str) -> int:
        if name == home.name:
            os.rename(name, moved.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            raise OSError("injected leaf-open failure")
        return real_open_directory_at(parent_fd, name)

    monkeypatch.setattr(generation_home, "_open_directory_at", replace_before_open)
    with pytest.raises(KeyGenerationError):
        PreparedGenerationHome.prepare(selection)

    assert moved.is_dir()
    assert home.is_dir()
    assert list(home.iterdir()) == []
