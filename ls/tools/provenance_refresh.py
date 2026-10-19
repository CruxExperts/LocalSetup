#!/usr/bin/env python3
"""Direct-execution wrapper for the scheduled provenance handoff."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ls.core.provenance_refresh.cli import main
from tools.qc_patrol.github_api import GitHubAPI
from tools.qc_patrol.issue_writer import write_issues


if __name__ == "__main__":
    raise SystemExit(main(ROOT, api=GitHubAPI.from_env(), issue_writer=write_issues))
