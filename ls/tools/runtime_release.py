#!/usr/bin/env python3
"""Prepare the complete runtime artifact set from a clean release checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ls.core.agent.release_assets_cli import main

if __name__ == '__main__':
    raise SystemExit(main())
