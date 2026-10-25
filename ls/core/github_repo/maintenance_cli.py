from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from .adapter import GitHubError
from .maintenance_service import (
    MaintenanceOperationError,
    verify,
)
from .maintenance_run import run
from .maintenance_setup import setup
from .state import JournalError, TargetOperationBusy


def _validate_selectors(args: Any) -> None:
    if args.maintenance_mode == "setup":
        if args.all_public:
            if not args.owner or not args.checkout_root:
                raise MaintenanceOperationError("setup --all-public requires --owner OWNER and --checkout-root ROOT")
            if args.repository:
                if not args.checkout:
                    raise MaintenanceOperationError("single-repository setup with --all-public membership check also requires --checkout PATH")
                if getattr(args, "config_directory", None):
                    raise MaintenanceOperationError("--config-directory is only for fleet setup planning without --repository")
            else:
                if args.checkout or args.config:
                    raise MaintenanceOperationError("fleet setup uses --checkout-root and optional --config-directory, not --checkout or --config")
        elif not args.repository or not args.checkout:
            raise MaintenanceOperationError("single-repository setup requires --repository OWNER/REPO and --checkout PATH")
        elif getattr(args, "config_directory", None):
            raise MaintenanceOperationError("--config-directory requires setup --all-public without --repository")
        elif args.owner or args.checkout_root:
            raise MaintenanceOperationError("--owner and --checkout-root require --all-public")
        if args.apply and args.config:
            raise MaintenanceOperationError("--config is a planning input; apply uses the saved private plan")
        return
    if args.all_public:
        if args.repository or args.checkout:
            raise MaintenanceOperationError("--all-public cannot be combined with --repository or --checkout")
        if not args.owner or not args.checkout_root:
            raise MaintenanceOperationError("--all-public requires --owner OWNER and --checkout-root ROOT")
    else:
        if not args.repository or not args.checkout:
            raise MaintenanceOperationError("single-repository mode requires --repository OWNER/REPO and --checkout PATH")
        if args.owner or args.checkout_root:
            raise MaintenanceOperationError("--owner and --checkout-root require --all-public")


def handle(args: Any, home: Path) -> int:
    try:
        _validate_selectors(args)
        if args.maintenance_mode == "setup":
            result = setup(args, home)
            status = result.get("status")
            code = 0 if status in {"planned", "complete", "already_applied"} else 2
        elif args.maintenance_mode == "verify":
            result = verify(args, home)
            code = 0 if result.get("status") == "verified" else 2
        elif args.maintenance_mode == "run":
            result = run(args, home)
            code = 0 if result.get("handoff_ready") is True else 2
        else:
            raise MaintenanceOperationError("unsupported github-maintenance mode")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except (MaintenanceOperationError, GitHubError, JournalError, TargetOperationBusy, ValueError, RuntimeError) as exc:
        print(f"localsetup: {exc}", file=sys.stderr)
        return 2
