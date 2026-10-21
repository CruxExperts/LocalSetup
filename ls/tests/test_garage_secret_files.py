from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ls/skills/ls-garage-s3-admin/scripts/lib"))

from ls_garage.reporting import ToolError
from ls_garage.secret_files import read_restore, write


def test_secret_export_is_exclusive_mode_0600_and_restore_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "key.json"
    write(str(path), {"accessKeyId": "id", "secretAccessKey": "secret", "name": "name"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_restore({"file": str(path)}) == {"accessKeyId": "id", "secretAccessKey": "secret", "name": "name"}
    with pytest.raises(ToolError, match="already exists"):
        write(str(path), {"accessKeyId": "other"})


def test_restore_rejects_wrong_mode_and_shape(tmp_path: Path) -> None:
    path = tmp_path / "key.json"; path.write_text(json.dumps({"accessKeyId": "id", "secretAccessKey": "secret"})); path.chmod(0o600)
    with pytest.raises(ToolError, match="exactly"):
        read_restore({"file": str(path)})
    path.chmod(0o644)
    with pytest.raises(ToolError, match="mode0600"):
        read_restore({"file": str(path)})
