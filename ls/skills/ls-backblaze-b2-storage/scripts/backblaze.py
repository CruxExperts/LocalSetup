#!/usr/bin/env python3
"""Standalone Backblaze B2 JSON operation CLI."""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(PACKAGE_LIB))
from ls_backblaze.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
