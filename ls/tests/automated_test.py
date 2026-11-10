#!/usr/bin/env python3
# Purpose: Minimal automated tests for framework paths and OS detection.
# Created: 2026-02-20
# Last updated: 2026-02-20

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from ls.lib.path_resolution import get_engine_dir, get_project_root, get_user_data_dir
from ls.discovery.core.os_detector import detect_os


def run_test(cond: bool, name: str) -> bool:
    if cond:
        print("[PASS]", name)
        return True
    print("[FAIL]", name)
    return False


def main() -> int:
    engine_dir = get_engine_dir()
    pass_count = 0
    fail_count = 0
    print("LocalSetup - Automated tests")
    print("===============================")
    if run_test(bool(get_engine_dir()), "get_engine_dir"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test(bool(get_user_data_dir()), "get_user_data_dir"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test(bool(get_project_root()), "get_project_root"):
        pass_count += 1
    else:
        fail_count += 1
    try:
        os_info = detect_os()
        if run_test(bool(os_info and "|" in os_info), "detect_os"):
            pass_count += 1
        else:
            fail_count += 1
    except Exception:
        if run_test(False, "detect_os"):
            pass_count += 1
        else:
            fail_count += 1
    if run_test((engine_dir / "lib" / "json_formatter.sh").is_file(), "json_formatter.sh"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test((engine_dir / "tools" / "localsetup.py").is_file(), "localsetup.py"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test((engine_dir / "skills").is_dir(), "skills dir"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test((engine_dir / "templates").is_dir(), "templates dir"):
        pass_count += 1
    else:
        fail_count += 1
    if run_test((engine_dir / "workflows").is_dir(), "workflows dir"):
        pass_count += 1
    else:
        fail_count += 1
    print("")
    print("Result: %d passed, %d failed" % (pass_count, fail_count))
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
