"""Identify LocalSetup-owned Linux processes without rewriting their arguments."""
from pathlib import Path
import sys

from .framework_version import framework_version


def set_process_name() -> bool:
    if sys.platform != "linux":
        return False
    name = "LS-v" + framework_version()
    try:
        # Linux comm is 15 bytes plus NUL. SemVer here is numeric ASCII.
        Path("/proc/self/comm").write_text(name[:15], encoding="ascii")
    except OSError:
        return False
    return True
