from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from ..paths import global_layout
from .adapter import GitHubError, GhCliAdapter
from .model import RepositoryTarget
from .planning import plan_markdown
from .policy import PolicyError, parse_target, read_policy
from .service import (
    ApplyError,
    PlanError,
    apply_plan,
    audit,
    create_plan,
    read_plan,
    verify_plan,
    write_plan_pair,
)
from .state import JournalError, TargetOperationBusy, operation_state_directory
from .verification import load_trusted_public_keys


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _print_markdown(title: str, value: Any) -> None:
    print(f"# {title}\n")
    for line in json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).splitlines():
        print(f"    {line}")


def _reject_unrelated_flags(args: Any, *, allowed: set[str]) -> None:
    values = {
        "policy": getattr(args, "policy", None),
        "plan": getattr(args, "plan", None),
        "authorize_plan": getattr(args, "authorize_plan", None),
        "operation": getattr(args, "operation", []),
        "output_directory": getattr(args, "output_directory", None),
        "trusted_public_key": getattr(args, "trusted_public_key", []),
    }
    unexpected = [name for name, value in values.items() if name not in allowed and value]
    if unexpected:
        raise PolicyError(f"flag(s) not used by --mode {args.mode}: {', '.join('--' + name.replace('_', '-') for name in unexpected)}")


def _plan_target(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "hostname": plan["target"]["hostname"],
        "repository_id": plan["target"]["repository_id"],
        "observed_full_name": plan["target"]["observed_full_name"],
        "actor_id": plan["target"]["actor_id"],
    }


def handle(args: Any, home: Path) -> int:
    try:
        target = parse_target(args.hostname, args.repository)
        adapter = GhCliAdapter(target)
        state_root = global_layout(home).state_root
        checkout_path = Path(args.checkout).expanduser()

        if args.mode == "audit":
            _reject_unrelated_flags(args, allowed=set())
            report = audit(adapter, target, checkout_path=checkout_path)
            if args.format == "markdown":
                _print_markdown("GitHub repository audit", report)
            else:
                _print_json(report)
            return 0

        if args.mode == "plan":
            _reject_unrelated_flags(args, allowed={"policy", "output_directory"})
            if not args.policy:
                raise PolicyError("--policy POLICY.json is required for --mode plan")
            policy = read_policy(Path(args.policy).expanduser())
            plan = create_plan(adapter, target, policy, checkout_path=checkout_path)
            if args.output_directory:
                output_directory = Path(args.output_directory)
            else:
                output_directory = (
                    operation_state_directory(state_root, target.hostname, plan["target"]["repository_id"])
                    / "plans"
                    / plan["plan_digest"]
                )
            json_path, markdown_path = write_plan_pair(plan, output_directory)
            _print_json({
                "status": "planned",
                "target": _plan_target(plan),
                "policy_digest": plan["policy_digest"],
                "plan_digest": plan["plan_digest"],
                "operation_ids": plan["operation_ids"],
                "report_only_count": len(plan["report_only"]),
                "plan_json": str(json_path),
                "plan_markdown": str(markdown_path),
            })
            return 0

        if args.mode == "apply":
            _reject_unrelated_flags(args, allowed={"plan", "authorize_plan", "operation"})
            if not args.plan:
                raise PolicyError("--plan PLAN.json is required for --mode apply")
            if not args.authorize_plan:
                raise PolicyError("--authorize-plan DIGEST is required for --mode apply")
            if not args.operation:
                raise PolicyError("apply requires one or more repeated --operation OPERATION_ID values")
            plan = read_plan(Path(args.plan).expanduser())
            result = apply_plan(
                plan,
                target,
                adapter,
                state_root,
                supplied_digest=args.authorize_plan,
                operation_ids=list(args.operation),
                checkout_path=checkout_path,
            )
            _print_json(result)
            return 0 if result.get("status") == "complete" else 2

        if args.mode == "verify":
            _reject_unrelated_flags(args, allowed={"plan", "trusted_public_key"})
            if not args.plan:
                raise PolicyError("--plan PLAN.json is required for --mode verify")
            plan = read_plan(Path(args.plan).expanduser())
            trusted_keys = load_trusted_public_keys(getattr(args, "trusted_public_key", []))
            result = verify_plan(plan, target, adapter, checkout_path=checkout_path, trusted_public_keys=trusted_keys)
            if args.format == "markdown":
                _print_markdown("GitHub repository verification", result)
            else:
                _print_json(result)
            return 0 if result.get("status") == "verified" else 2

        raise PolicyError("unsupported github-repo mode")
    except (PolicyError, PlanError, ApplyError, GitHubError, JournalError, TargetOperationBusy, RuntimeError, ValueError) as exc:
        print(f"localsetup: {exc}", file=sys.stderr)
        return 2
