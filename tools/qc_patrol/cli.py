from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ls" / "lib"))
from deps import require_deps

require_deps(["jsonschema", "requests", "yaml"])

from tools.qc_patrol.chunking import chunk_text
from tools.qc_patrol.adjudication import (
    adjudicate_packets,
    ai_findings_from_adjudications,
    empty_packet_dispositions,
    load_packet_dispositions,
    rule_suggestions_from_adjudications,
    write_packet_dispositions,
)
from tools.qc_patrol.config import LLMConfig, load_config, patrol_llm_config
from tools.qc_patrol.deterministic_checks import run_deterministic
from tools.qc_patrol.diff_reader import fetch_ref, read_diff
from tools.qc_patrol.docs_drift import docs_alignment_findings
from tools.qc_patrol.drift import build_drift_packets, load_baseline, load_pending_packets, merge_pending_packets, pending_packets_payload, summarize_drift
from tools.qc_patrol.issue_writer import write_issues
from tools.qc_patrol.ledger import build_ledger, build_ledger_v2, write_json
from tools.qc_patrol.llm_client import LLMClient, LLMDisabled
from tools.qc_patrol.markdown_versions import markdown_version_packets
from tools.qc_patrol.pr_writer import plan_autofix
from tools.qc_patrol.redaction import redact_text
from tools.qc_patrol.release_writer import release_readiness
from tools.qc_patrol.repo_inventory import build_inventory
from tools.qc_patrol.review_contracts import parse_strict_json
from tools.qc_patrol.schemas import DRIFT_PACKETS_SCHEMA, validate_payload


def build_pr_review_prompt(chunks: list[dict[str, object]], findings: list[dict[str, object]]) -> str:
    payload = {
        "task": "Review the supplied pull request diff chunks and deterministic findings for actionable repository QC issues.",
        "output_contract": {
            "format": "strict_json_only",
            "schema": {
                "findings": [
                    {
                        "category": "workflow_security|release|docs|pr_review|other_snake_case",
                        "severity": "low|medium|high|critical",
                        "title": "short finding title, max 140 characters",
                        "body": "concise rationale with evidence",
                        "affected_paths": ["path/from/repo/root"],
                        "region": "optional file region",
                        "check_type": "llm_pr_review",
                        "remediation": "optional suggested fix",
                    }
                ]
            },
            "empty_result": {"findings": []},
            "rules": [
                "Return only one JSON object.",
                "Do not wrap the JSON in markdown fences.",
                "Do not include prose before or after the JSON.",
                "If there are no actionable findings, return exactly {\"findings\":[]}.",
                "Do not include secrets, raw credentials, or endpoint URLs.",
            ],
        },
        "chunks": chunks,
        "deterministic_findings": findings,
    }
    return json.dumps(payload, sort_keys=True)


def _repo(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", ".")).resolve()


def _patrol_runtime_status(config: LLMConfig) -> str:
    from ls.core.agent.diagnostics import locations
    from ls.core.agent.runtime_diagnostics import runtime

    runtime_root = Path(config.runtime_root or locations(Path.home())["runtimes"]).expanduser()
    return str(runtime(runtime_root).get("status", "unavailable"))


def cmd_inventory(args: argparse.Namespace) -> int:
    write_json(Path(args.out), build_inventory(_repo(args)))
    return 0


def cmd_deterministic(args: argparse.Namespace) -> int:
    findings = run_deterministic(_repo(args), args.profile)
    write_json(Path(args.out), build_ledger("deterministic", findings, profile=args.profile))
    return 0


def cmd_pr_review(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    repo = Path(args.subject_repo).resolve()
    config = load_config(Path.cwd())
    if args.base_remote:
        fetch_ref(repo, args.base_remote, args.base)
    diff = read_diff(repo, args.base, args.head)
    chunks = chunk_text("pr-diff", diff, config.max_chunk_bytes)
    findings = run_deterministic(repo, "ci")
    if args.llm_mode != "off":
        try:
            response = LLMClient(config.llm).complete(build_pr_review_prompt(chunks, findings))
            findings.extend(parse_strict_json(response, out / "llm-raw-response.json").get("findings", []))
        except LLMDisabled:
            pass
        except Exception as exc:
            write_json(out / "llm-error.json", {"error": redact_text(str(exc)), "endpoint_alias": config.llm.endpoint_alias})
    write_json(out / "ledger.json", build_ledger("pr-review", findings, chunk_count=len(chunks), llm_mode=args.llm_mode))
    return 0


def cmd_patrol(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = load_config(repo)
    inventory = build_inventory(repo)
    write_json(out / "inventory.json", inventory)
    baseline = load_baseline(Path(args.baseline).resolve() if args.baseline else None)
    prior_pending = load_pending_packets(Path(args.pending_packets).resolve() if args.pending_packets else None)
    dispositions: dict[str, str] | None = None
    disposition_error = ""
    try:
        if args.dispositions and args.initialize_dispositions:
            raise ValueError("QC packet dispositions cannot be loaded and initialized together")
        if args.dispositions:
            dispositions = load_packet_dispositions(Path(args.dispositions).resolve())
        elif args.initialize_dispositions and args.ai_mode == "off":
            dispositions = empty_packet_dispositions()
        elif args.initialize_dispositions:
            raise ValueError("QC packet dispositions may be initialized only in deterministic mode")
        else:
            raise ValueError("QC packet disposition artifact is required; explicit deterministic initialization is required")
        write_packet_dispositions(out / "packet-dispositions.json", dispositions)
    except ValueError as exc:
        disposition_error = redact_text(str(exc))

    drift_packets = build_drift_packets(inventory, baseline, repo_root=repo)
    markdown_packets = markdown_version_packets(repo, inventory)
    drift_packets["packets"].extend(markdown_packets["packets"])
    drift_packets = merge_pending_packets(drift_packets, prior_pending, inventory, dispositions or {})
    drift_errors = validate_payload(drift_packets, DRIFT_PACKETS_SCHEMA)
    if drift_errors:
        raise ValueError("invalid merged QC drift packets: " + "; ".join(drift_errors))
    write_json(out / "drift-packets.json", drift_packets)
    adjudication_packets = [
        packet for packet in drift_packets["packets"] if packet.get("kind") != "shape.no_baseline"
    ]
    write_json(out / "pending-packets.json", pending_packets_payload(adjudication_packets))
    ai_input = {"schema_version": drift_packets["schema_version"], "packets": adjudication_packets}
    ai_adjudications: dict[str, Any] = {
        "schema_version": "qc.ai-adjudication.v1",
        "adjudications": [],
        "errors": ([{"packet_id": "packet-dispositions", "error": disposition_error}] if disposition_error else []),
        "pending_packets": adjudication_packets,
        "calls_made": 0,
    }
    selected_model: str | None = None
    selected_effort = ""
    if args.ai_mode == "packets" and not disposition_error:
        if baseline is None:
            ai_adjudications["errors"] = [
                {
                    "packet_id": "patrol-baseline",
                    "error": "A successful inventory baseline is required; no provider request was made.",
                }
            ]
        else:
            try:
                patrol_config = patrol_llm_config(config.llm)
                selected_model = patrol_config.model
                selected_effort = patrol_config.reasoning_effort
                if not patrol_config.base_url or not patrol_config.api_key:
                    raise ValueError("QC LLM endpoint and credential must be configured; no provider request was made")
                runtime_status = _patrol_runtime_status(patrol_config)
                if runtime_status != "verified":
                    raise ValueError(f"QC protected completion runtime is unavailable ({runtime_status}); no provider request was made")
                if adjudication_packets:
                    ai_adjudications = adjudicate_packets(
                        ai_input,
                        LLMClient(patrol_config),
                        out,
                        dispositions=dispositions,
                    )
            except Exception as exc:
                ai_adjudications["errors"].append(
                    {"packet_id": "patrol-route", "error": redact_text(str(exc))}
                )
                ai_adjudications["pending_packets"] = adjudication_packets
    write_json(out / "ai-adjudications.json", ai_adjudications)
    pending = ai_adjudications.get("pending_packets", [])
    write_json(out / "pending-packets.json", pending_packets_payload(pending if isinstance(pending, list) else []))
    rule_suggestions = rule_suggestions_from_adjudications(ai_adjudications)
    write_json(out / "rule-suggestions.json", rule_suggestions)
    findings = run_deterministic(repo, "ci")
    ai_findings = ai_findings_from_adjudications(ai_adjudications)
    issue_results = (
        write_issues(findings, config.labels, dry_run=not args.write_issues, policy_mode="conservative")
        if findings
        else []
    )
    artifacts = {
        "inventory": "inventory.json",
        "drift_packets": "drift-packets.json",
        "ai_adjudications": "ai-adjudications.json",
        "pending_packets": "pending-packets.json",
        "rule_suggestions": "rule-suggestions.json",
        "ledger": "ledger.json",
    }
    if dispositions is not None:
        artifacts["packet_dispositions"] = "packet-dispositions.json"
    ledger = build_ledger_v2(
        "patrol",
        findings,
        head_sha=str(inventory.get("head_sha", "")),
        ai_mode=args.ai_mode,
        ai_findings=ai_findings,
        issue_results=issue_results,
        artifacts=artifacts,
        baseline={"path": args.baseline, "loaded": baseline is not None},
        inventory_summary={
            "tracked_file_count": inventory.get("tracked_file_count", 0),
            "docs": len((inventory.get("surfaces") or {}).get("docs", [])),
            "workflows": len((inventory.get("surfaces") or {}).get("workflows", [])),
        },
        drift_summary=summarize_drift(drift_packets),
        packet_disposition_status="available" if dispositions is not None else "unavailable",
        packet_disposition_count=len(dispositions) if dispositions is not None else None,
        pending_packet_count=len(pending) if isinstance(pending, list) else 0,
        ai_calls_made=int(ai_adjudications.get("calls_made", 0)),
        ai_route={
            "model": selected_model,
            "reasoning_effort": selected_effort or "not-declared-low",
            "output_token_cap": 1000,
            "per_run_call_cap": 4,
        },
        rule_suggestions=rule_suggestions,
    )
    write_json(out / "ledger.json", ledger)
    return 1 if ai_adjudications.get("errors") else 0


def cmd_docs_drift(args: argparse.Namespace) -> int:
    repo = _repo(args)
    findings = docs_alignment_findings(repo)
    actionable = [finding for finding in findings if finding.get("check_type") == "docs_alignment"]
    issue_results = write_issues(actionable, load_config(repo).labels, dry_run=not args.write_issues) if actionable else []
    write_json(Path(args.out) / "ledger.json", build_ledger("docs-drift", findings, issue_results=issue_results))
    return 1 if any(finding.get("check_type") == "docs_alignment_unavailable" for finding in findings) else 0


def cmd_release_readiness(args: argparse.Namespace) -> int:
    result = release_readiness(_repo(args), Path(args.out))
    write_json(Path(args.out) / "ledger.json", build_ledger("release-readiness", result["findings"], release=result))
    return 0 if result["package_returncode"] == 0 and result["verify_returncode"] == 0 else 1


def cmd_autofix(args: argparse.Namespace) -> int:
    result = plan_autofix(args.issue, dry_run=args.dry_run == "true", create_pr=args.create_pr == "true", out=Path(args.out))
    write_json(Path(args.out) / "ledger.json", build_ledger("autofix", [], autofix=result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LocalSetup GitHub QC patrol")
    sub = parser.add_subparsers(required=True)
    inventory = sub.add_parser("inventory")
    inventory.add_argument("--repo", default=".")
    inventory.add_argument("--out", required=True)
    inventory.set_defaults(func=cmd_inventory)
    deterministic = sub.add_parser("deterministic")
    deterministic.add_argument("--repo", default=".")
    deterministic.add_argument("--profile", choices=["ci", "docs", "release"], required=True)
    deterministic.add_argument("--out", required=True)
    deterministic.set_defaults(func=cmd_deterministic)
    pr_review = sub.add_parser("pr-review")
    pr_review.add_argument("--subject-repo", required=True)
    pr_review.add_argument("--base", required=True)
    pr_review.add_argument("--head", required=True)
    pr_review.add_argument("--base-remote", default="")
    pr_review.add_argument("--out", required=True)
    pr_review.add_argument("--llm-mode", choices=["auto", "off"], default="auto")
    pr_review.set_defaults(func=cmd_pr_review)
    patrol = sub.add_parser("patrol")
    patrol.add_argument("--repo", default=".")
    patrol.add_argument("--out", required=True)
    patrol.add_argument("--write-issues", action="store_true")
    patrol.add_argument("--baseline", default="")
    patrol.add_argument("--pending-packets", default="")
    patrol.add_argument("--dispositions", default="")
    patrol.add_argument("--initialize-dispositions", action="store_true")
    patrol.add_argument("--ai-mode", choices=["off", "packets"], default="off")
    patrol.set_defaults(func=cmd_patrol)
    for name, func in [("docs-drift", cmd_docs_drift)]:
        command = sub.add_parser(name)
        command.add_argument("--repo", default=".")
        command.add_argument("--out", required=True)
        command.add_argument("--write-issues", action="store_true")
        command.set_defaults(func=func)
    release = sub.add_parser("release-readiness")
    release.add_argument("--repo", default=".")
    release.add_argument("--out", required=True)
    release.set_defaults(func=cmd_release_readiness)
    autofix = sub.add_parser("autofix")
    autofix.add_argument("--issue", required=True)
    autofix.add_argument("--dry-run", choices=["true", "false"], required=True)
    autofix.add_argument("--create-pr", choices=["true", "false"], required=True)
    autofix.add_argument("--out", required=True)
    autofix.set_defaults(func=cmd_autofix)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
