#!/usr/bin/env python3
# Purpose: Verify framework context skill exists (canonical source).
# Created: 2026-02-20
# Last updated: 2026-04-01

import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE))
from lib.path_resolution import get_project_root


def main():
    root = get_project_root()
    skill_md = root / "ls" / "skills" / "ls-localsetup-project-context" / "SKILL.md"
    print("LocalSetup - Context Verification")
    print("=====================================")
    if skill_md.is_file():
        print(
            "[OK] ls/skills/ls-localsetup-project-context/SKILL.md exists (%s bytes)"
            % skill_md.stat().st_size
        )
    else:
        sys.stderr.write(
            "[FAIL] No ls/skills/ls-localsetup-project-context/SKILL.md found. Framework not properly installed.\n"
        )
        return 1
    print("[OK] Context verification complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
