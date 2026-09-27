#!/usr/bin/env python3
"""Probe GitHub Actions evidence for a specific commit."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ls.core.github_repo.ci_evidence import main

if __name__ == "__main__":
    raise SystemExit(main())
