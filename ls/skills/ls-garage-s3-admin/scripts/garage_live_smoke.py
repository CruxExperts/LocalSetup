#!/usr/bin/env python3
"""Opt-in, read-only Garage configuration smoke; never runs by default."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in read-only Garage S3 smoke")
    parser.add_argument("--apply", action="store_true", help="acknowledge that this contacts the configured provider")
    parser.add_argument("--config", required=True)
    parser.add_argument("--admin-health", action="store_true", help="also query the separately configured Admin health endpoint")
    parser.add_argument("--bucket", help="optionally also issue HeadBucket")
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required; this script otherwise makes no network request")
    tool = Path(__file__).with_name("garage.py")
    commands = [[sys.executable, str(tool), "--tool", "s3.ListBuckets", "--args-json", "{}", "--config", args.config, "--apply"]]
    if args.bucket:
        commands.append([sys.executable, str(tool), "--tool", "s3.HeadBucket", "--args-json", json.dumps({"bucket": args.bucket}), "--config", args.config, "--apply"])
    if args.admin_health:
        commands.append([sys.executable, str(tool), "--tool", "admin.GetClusterHealth", "--args-json", "{}", "--config", args.config, "--apply"])
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode: return result.returncode
    return 0


if __name__ == "__main__": raise SystemExit(main())
