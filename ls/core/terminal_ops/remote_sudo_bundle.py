"""Package the installed operation-bound sudo runtime for remote execution."""
from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
import zipfile

from . import sudo as sudo_api


def sudo_runtime_bundle() -> tuple[str, str]:
    """Return a nonsecret installed-runtime ZIP and its SHA-256 digest."""
    package = Path(sudo_api.__file__).resolve().parent
    ls_root = package.parents[1]
    members = [package / name for name in (
        "sudo.py", "credentials.py", "askpass.py", "sudo_channel.py",
        "dialog_channel.py", "command_state.py", "dialog_model.py", "shell.py", "models.py")]
    askpass = ls_root / "tools" / "sudo_askpass"
    if not askpass.is_file() or not os.access(askpass, os.X_OK):
        raise RuntimeError("installed LocalSetup askpass helper is unavailable")
    encoded = io.BytesIO()
    with zipfile.ZipFile(encoded, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # The remote runtime does not import the full pane backend package.
        archive.writestr("ls/__init__.py", "")
        archive.writestr("ls/core/__init__.py", "")
        archive.writestr("ls/core/terminal_ops/__init__.py", "")
        for path in (*members, askpass):
            if not path.is_file() or path.is_symlink():
                raise RuntimeError("installed LocalSetup terminal source is incomplete")
            relative = path.relative_to(ls_root.parent).as_posix()
            if len(path.read_bytes()) > 256 * 1024:
                raise RuntimeError("installed LocalSetup terminal source exceeds limit")
            archive.write(path, relative)
    bundle = encoded.getvalue()
    if len(bundle) > 256 * 1024:
        raise RuntimeError("installed LocalSetup terminal bundle exceeds limit")
    return base64.b64encode(bundle).decode("ascii"), hashlib.sha256(bundle).hexdigest()
