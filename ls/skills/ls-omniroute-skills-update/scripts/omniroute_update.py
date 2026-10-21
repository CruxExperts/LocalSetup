#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

for parent in Path(__file__).resolve().parents:
    if (parent / "lib" / "deps.py").is_file():
        sys.path.insert(0, str(parent / "lib"))
        from deps import require_deps

        require_deps(["yaml"])
        break

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guidance
    raise SystemExit("Missing dependency: PyYAML. Run `uv sync --locked --no-dev` from the LocalSetup source checkout.") from exc


DEFAULT_SOURCE_REPO = "https://github.com/diegosouzapw/OmniRoute.git"
DEFAULT_REF = "5458026c216f77a3da68ea49152dc33470cfe2cb"
CONVERTER_VERSION = "1.0"
NATIVE_COVERAGE: dict[str, list[str]] = {
    "cli-a2a": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-backup-sync": ["ls-omniroute-admin-automation"],
    "cli-batches": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-chat": ["ls-omniroute-model-discovery"],
    "cli-compression": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-contexts": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-cost-usage": ["ls-omniroute-model-discovery"],
    "cli-eval": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-health": ["ls-omniroute-model-discovery"],
    "cli-keys": ["ls-omniroute-admin-automation"],
    "cli-mcp": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-models": ["ls-omniroute-model-discovery"],
    "cli-plugins-skills": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-policy-audit": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-providers": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-resilience": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-routing": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "cli-serve": ["ls-omniroute-task-triage", "ls-omniroute-admin-automation"],
    "cli-setup": ["ls-omniroute-task-triage", "ls-omniroute-admin-automation"],
    "cli-tunnel": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "config-codex-cli": ["ls-omniroute-task-triage", "ls-omniroute-admin-automation"],
    "omni-agents-a2a": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-api-keys": ["ls-omniroute-admin-automation"],
    "omni-auth": ["ls-omniroute-task-triage", "ls-omniroute-admin-automation"],
    "omni-budget": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-cache": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-cli-tools": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-combos-routing": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-compression": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-context-rtk": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-db-backups": ["ls-omniroute-admin-automation"],
    "omni-github-skills": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-inference": ["ls-omniroute-model-discovery"],
    "omni-mcp": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-models": ["ls-omniroute-model-discovery"],
    "omni-providers": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-proxies": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-resilience": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-settings": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-sync-cloud": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-tunnels": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
    "omni-usage-logs": ["ls-omniroute-model-discovery"],
    "omni-version-manager": ["ls-omniroute-admin-automation", "ls-omniroute-task-triage"],
    "omni-webhooks": ["ls-omniroute-model-discovery", "ls-omniroute-admin-automation"],
}


class ConverterError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpstreamSkill:
    name: str
    path: str
    sha256: str
    source_commit: str | None
    source_commit_date: str | None


@dataclass(frozen=True)
class LocalSkill:
    name: str
    path: str
    metadata: dict[str, Any]
    omniroute: dict[str, Any]
    tags: list[str]


@dataclass(frozen=True)
class ReportRow:
    status: str
    upstream_skill: str | None
    local_skill: str | None
    intended_local: str | None
    source_path: str | None
    detail: str


def upstream_to_local_name(upstream_name: str) -> str:
    return f"ls-{upstream_name}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    data = yaml.safe_load(text[4:end])
    return data if isinstance(data, dict) else {}


def _run_git(args: list[str], cwd: Path | None, timeout: int) -> str:
    if not shutil.which("git"):
        raise ConverterError("git is required when --source-path is not provided")
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ConverterError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def _git_metadata(repo_path: Path, timeout: int) -> tuple[str | None, str | None]:
    try:
        commit = _run_git(["rev-parse", "HEAD"], repo_path, timeout)
        commit_date = _run_git(["show", "-s", "--format=%cI", "HEAD"], repo_path, timeout)
    except (ConverterError, subprocess.TimeoutExpired):
        return None, None
    return commit or None, commit_date or None


def _clone_source(source_repo: str, ref: str, clone_root: Path, timeout: int) -> None:
    try:
        _run_git(["clone", "--depth", "1", "--branch", ref, source_repo, str(clone_root)], None, timeout)
        return
    except ConverterError:
        if clone_root.exists():
            shutil.rmtree(clone_root)

    _run_git(["clone", source_repo, str(clone_root)], None, timeout)
    _run_git(["checkout", ref], clone_root, timeout)


def _resolve_source_root(source_path: Path | None, source_repo: str, ref: str, timeout: int) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if source_path is not None:
        root = source_path.expanduser().resolve()
        if not root.exists():
            raise ConverterError(f"--source-path does not exist: {root}")
        return root, None

    tempdir = tempfile.TemporaryDirectory(prefix="omniroute-skills-")
    clone_root = Path(tempdir.name) / "source"
    _clone_source(source_repo, ref, clone_root, timeout)
    return clone_root, tempdir


def _skills_root(source_root: Path) -> Path:
    if (source_root / "skills").is_dir():
        return source_root / "skills"
    if source_root.name == "skills" and source_root.is_dir():
        return source_root
    if any(path.name == "SKILL.md" for path in source_root.glob("*/SKILL.md")):
        return source_root
    raise ConverterError(f"could not find OmniRoute skills root under {source_root}")


def read_upstream_skills(
    *,
    source_path: Path | None,
    source_repo: str,
    ref: str,
    timeout: int,
    include_regex: str | None = None,
) -> tuple[list[UpstreamSkill], dict[str, Any]]:
    pattern = re.compile(include_regex) if include_regex else None
    source_root, tempdir = _resolve_source_root(source_path, source_repo, ref, timeout)
    try:
        skills_root = _skills_root(source_root)
        commit, commit_date = _git_metadata(source_root if (source_root / ".git").exists() else skills_root.parent, timeout)
        rows: list[UpstreamSkill] = []
        for skill_md in sorted(skills_root.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if pattern and not pattern.search(name):
                continue
            text = skill_md.read_text(encoding="utf-8")
            rows.append(
                UpstreamSkill(
                    name=name,
                    path=str(skill_md.relative_to(skills_root.parent)),
                    sha256=sha256_text(text),
                    source_commit=commit,
                    source_commit_date=commit_date,
                )
            )
        return rows, {
            "source_repo": source_repo,
            "source_ref": ref,
            "source_root": str(source_root),
            "skills_root": str(skills_root),
            "source_commit": commit,
            "source_commit_date": commit_date,
        }
    finally:
        if tempdir is not None:
            tempdir.cleanup()


def _taxonomy_tags(repo_root: Path) -> dict[str, list[str]]:
    pack_path = repo_root / "ls" / "config" / "pack.yaml"
    if not pack_path.exists():
        return {}
    data = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    taxonomy = data.get("extensions", {}).get("skill_taxonomy", {})
    if not isinstance(taxonomy, dict):
        return {}
    out: dict[str, list[str]] = {}
    for skill_name, row in taxonomy.items():
        if isinstance(row, dict) and isinstance(row.get("tags"), list):
            out[str(skill_name)] = [str(tag) for tag in row["tags"]]
    return out


def read_local_skills(repo_root: Path) -> list[LocalSkill]:
    skills_root = repo_root / "ls" / "skills"
    if not skills_root.is_dir():
        raise ConverterError(f"missing LocalSetup skills root: {skills_root}")
    tags_by_skill = _taxonomy_tags(repo_root)
    rows: list[LocalSkill] = []
    for skill_md in sorted(skills_root.glob("ls-*/SKILL.md")):
        frontmatter = parse_frontmatter(skill_md)
        name = str(frontmatter.get("name") or skill_md.parent.name)
        extensions = frontmatter.get("extensions", {})
        omniroute = extensions.get("omniroute", {}) if isinstance(extensions, dict) else {}
        rows.append(
            LocalSkill(
                name=name,
                path=str(skill_md.relative_to(repo_root)),
                metadata=frontmatter,
                omniroute=omniroute if isinstance(omniroute, dict) else {},
                tags=tags_by_skill.get(name, []),
            )
        )
    return rows


def _is_omniroute_tagged(local: LocalSkill) -> bool:
    return local.name.startswith("ls-omniroute-task-triage") or "omniroute" in local.tags or bool(local.omniroute)


def _source_kind(local: LocalSkill) -> str | None:
    value = local.omniroute.get("source_kind")
    return str(value) if isinstance(value, str) and value else None


def _is_local_native(local: LocalSkill) -> bool:
    return _source_kind(local) == "localsetup-native"


def classify(upstream: list[UpstreamSkill], local: list[LocalSkill]) -> list[ReportRow]:
    upstream_by_name = {row.name: row for row in upstream}
    local_by_source: dict[str, LocalSkill] = {}
    local_by_name = {row.name: row for row in local}
    rows: list[ReportRow] = []

    for item in local:
        source_skill = item.omniroute.get("source_skill")
        if isinstance(source_skill, str) and source_skill:
            local_by_source[source_skill] = item

    for source in upstream:
        local_item = local_by_source.get(source.name)
        intended = upstream_to_local_name(source.name)
        if local_item is None:
            coverage = [
                skill_name
                for skill_name in NATIVE_COVERAGE.get(source.name, [])
                if skill_name in local_by_name and _is_local_native(local_by_name[skill_name])
            ]
            if coverage:
                rows.append(
                    ReportRow(
                        status="covered-native",
                        upstream_skill=source.name,
                        local_skill=", ".join(coverage),
                        intended_local=", ".join(coverage),
                        source_path=source.path,
                        detail="covered by consolidated LocalSetup-native OmniRoute skill",
                    )
                )
                continue
            rows.append(
                ReportRow(
                    status="missing-local",
                    upstream_skill=source.name,
                    local_skill=None,
                    intended_local=intended,
                    source_path=source.path,
                    detail="no local converted skill declares this upstream source",
                )
            )
            continue

        local_sha = str(local_item.omniroute.get("source_sha256", ""))
        local_commit = str(local_item.omniroute.get("source_commit", ""))
        commit_matches = not source.source_commit or local_commit == source.source_commit
        hash_matches = local_sha == source.sha256
        rows.append(
            ReportRow(
                status="current" if hash_matches and commit_matches else "stale-local",
                upstream_skill=source.name,
                local_skill=local_item.name,
                intended_local=intended,
                source_path=source.path,
                detail="source metadata matches" if hash_matches and commit_matches else "source hash or commit differs",
            )
        )

    for source_skill, local_item in sorted(local_by_source.items()):
        if source_skill not in upstream_by_name:
            rows.append(
                ReportRow(
                    status="local-only",
                    upstream_skill=source_skill,
                    local_skill=local_item.name,
                    intended_local=upstream_to_local_name(source_skill),
                    source_path=str(local_item.omniroute.get("source_path") or ""),
                    detail="local converted skill claims an upstream source that was not found",
                )
            )

    for item in local:
        source_skill = item.omniroute.get("source_skill")
        if not source_skill and _is_omniroute_tagged(item):
            if _is_local_native(item):
                local_role = item.omniroute.get("local_role")
                detail = "local LocalSetup-native OmniRoute skill"
                if isinstance(local_role, str) and local_role:
                    detail = f"{detail}: {local_role}"
                rows.append(
                    ReportRow(
                        status="local-native",
                        upstream_skill=None,
                        local_skill=item.name,
                        intended_local=None,
                        source_path=None,
                        detail=detail,
                    )
                )
                continue
            rows.append(
                ReportRow(
                    status="untracked-local",
                    upstream_skill=None,
                    local_skill=item.name,
                    intended_local=None,
                    source_path=None,
                    detail="local OmniRoute-tagged skill lacks extensions.omniroute.source_skill",
                )
            )

    order = {
        "missing-local": 0,
        "stale-local": 1,
        "current": 2,
        "covered-native": 3,
        "local-only": 4,
        "untracked-local": 5,
        "local-native": 6,
    }
    return sorted(rows, key=lambda row: (order.get(row.status, 99), row.upstream_skill or "", row.local_skill or ""))


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).expanduser().resolve()
    upstream, source = read_upstream_skills(
        source_path=Path(args.source_path) if args.source_path else None,
        source_repo=args.source_repo,
        ref=args.ref,
        timeout=args.timeout,
        include_regex=args.include_regex,
    )
    local = read_local_skills(repo_root)
    rows = classify(upstream, local)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    freshness = freshness_summary(
        rows,
        require_all_upstream=getattr(args, "require_all_upstream", False),
        strict_untracked=getattr(args, "strict_untracked", False),
    )
    return {
        "schema_version": 1,
        "converter_version": CONVERTER_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(repo_root),
        "source": source,
        "summary": {
            "upstream_count": len(upstream),
            "local_omniroute_count": sum(1 for item in local if _is_omniroute_tagged(item)),
            "status_counts": counts,
            "freshness": freshness,
        },
        "rows": [asdict(row) for row in rows],
    }


def freshness_summary(rows: list[ReportRow], *, require_all_upstream: bool = False, strict_untracked: bool = False) -> dict[str, Any]:
    blocking_statuses = {"stale-local", "local-only"}
    if require_all_upstream:
        blocking_statuses.add("missing-local")
    if strict_untracked:
        blocking_statuses.add("untracked-local")
    blocking_rows = [row for row in rows if row.status in blocking_statuses]
    return {
        "fresh": not blocking_rows,
        "blocking_statuses": sorted(blocking_statuses),
        "blocking_count": len(blocking_rows),
        "blocking_rows": [asdict(row) for row in blocking_rows],
    }


def render_markdown(report: dict[str, Any]) -> str:
    source = report["source"]
    freshness = report["summary"].get("freshness", {})
    lines = [
        "# Omni Route Update Report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Source repo: `{source['source_repo']}`",
        f"- Source ref: `{source['source_ref']}`",
        f"- Source commit: `{source.get('source_commit') or 'unknown'}`",
        f"- Upstream skills: `{report['summary']['upstream_count']}`",
        f"- Freshness: `{'fresh' if freshness.get('fresh') else 'not fresh'}`",
        f"- Freshness blockers: `{freshness.get('blocking_count', 0)}`",
        "",
        "| status | upstream | local | intended local | source path | detail |",
        "|---|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        lines.append(
            "| {status} | {upstream} | {local} | {intended} | {source_path} | {detail} |".format(
                status=row["status"],
                upstream=row["upstream_skill"] or "",
                local=row["local_skill"] or "",
                intended=row["intended_local"] or "",
                source_path=row["source_path"] or "",
                detail=str(row["detail"]).replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def write_output(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text, end="" if text.endswith("\n") else "\n")


def check_command(args: argparse.Namespace) -> int:
    report = build_report(args)
    if args.output == "json":
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        text = render_markdown(report)
    write_output(text, args.out)
    return 0


def freshness_command(args: argparse.Namespace) -> int:
    report = build_report(args)
    if args.output == "json":
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        text = render_markdown(report)
    write_output(text, args.out)
    return 0 if report["summary"]["freshness"]["fresh"] else 1


def add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", default=".", help="LocalSetup checkout to compare against.")
    parser.add_argument("--source-repo", default=DEFAULT_SOURCE_REPO, help="OmniRoute Git repository URL.")
    parser.add_argument("--source-path", help="Local OmniRoute checkout or exported skills root for offline checks.")
    parser.add_argument("--ref", default=DEFAULT_REF, help="Upstream branch, tag, or commit.")
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown", help="Report format.")
    parser.add_argument("--out", help="Optional output file. Defaults to stdout.")
    parser.add_argument("--timeout", type=int, default=30, help="Network or git timeout in seconds.")
    parser.add_argument("--include-regex", help="Optional upstream skill-name regex filter.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only OmniRoute update reporter.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", help="Compare upstream OmniRoute skills with local converted skills.")
    add_source_arguments(check)
    check.set_defaults(func=check_command)

    freshness = subparsers.add_parser(
        "freshness",
        help="Validate converted local OmniRoute skills against upstream source metadata.",
    )
    add_source_arguments(freshness)
    freshness.add_argument(
        "--require-all-upstream",
        action="store_true",
        help="Treat missing local conversions for upstream skills as freshness failures.",
    )
    freshness.add_argument(
        "--strict-untracked",
        action="store_true",
        help="Treat untracked local OmniRoute skills without source metadata as freshness failures.",
    )
    freshness.set_defaults(func=freshness_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConverterError, OSError, subprocess.TimeoutExpired, re.error, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
