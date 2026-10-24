"""Release assembly command; the installed runtime remains owned by setup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import zipfile

from .release_assets import prepare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--native-directory', type=Path,
                        help='Use already downloaded pinned assets instead of GitHub')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.repo_root.absolute(), args.wheel.absolute(), args.out.absolute(),
                         native_directory=args.native_directory)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        # Download diagnostics may include authenticated transport URLs.
        result = {'ok': False, 'error': str(exc) if isinstance(exc, ValueError)
                  else 'Runtime release assembly failed; inspect inputs and retained output'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1
