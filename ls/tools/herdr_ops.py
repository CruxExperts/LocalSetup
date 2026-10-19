#!/usr/bin/env python3
"""Direct-execution wrapper for LocalSetup's Herdr operations CLI."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ls.core.herdr_ops.cli import main


if __name__ == "__main__":
    raise SystemExit(main(root=ROOT))
