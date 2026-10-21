#!/usr/bin/env python3
"""Standalone Garage JSON operation boundary."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ls_garage.cli import main
raise SystemExit(main())
