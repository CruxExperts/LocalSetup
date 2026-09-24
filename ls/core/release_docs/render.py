"""Render release record content into the owned public-document sections."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .content import validate_record


_BLOCKS = {
    "README.md": "release-summary",
    "ls/README.md": "release-link",
    "ls/docs/README.md": "release-link",
}


def _replace_block(text: str, name: str, replacement: str) -> str:
    start = f"<!-- {name}:start -->"
    end = f"<!-- {name}:end -->"
    first = text.find(start)
    last = text.find(end)
    if first < 0 or last < 0 or last < first:
        raise ValueError(f"Required managed block is missing: {name}")
    if text.find(start, first + len(start)) >= 0 or text.find(end, last + len(end)) >= 0:
        raise ValueError(f"Managed block is duplicated: {name}")
    return text[:first] + replacement + text[last + len(end):]


def _release_url(record: dict[str, Any]) -> str:
    return f"ls/docs/releases/{record['version']}.md"


def _summary_block(record: dict[str, Any]) -> str:
    lines = ["<!-- release-summary:start -->", f"## What's new in {record['version']}", "", record["summary"], ""]
    lines.extend(f"- {row['text']}" for row in record["highlights"])
    lines.extend(["", f"See the [{record['version']} release guide]({_release_url(record)}) for compatibility, updating, and verification.", "<!-- release-summary:end -->"])
    return "\n".join(lines)


def _link_block(record: dict[str, Any], *, guide: str) -> str:
    return "\n".join([
        "<!-- release-link:start -->",
        f"Read the [current release guide]({guide}) for LocalSetup {record['version']}, including compatibility, updating, and verification. Find downloads in the [latest published release](https://github.com/CruxExperts/localsetup/releases/latest).",
        "<!-- release-link:end -->",
    ])


def _section(title: str, values: list[str]) -> list[str]:
    lines = [f"## {title}", ""]
    for value in values:
        lines.extend([value, ""])
    return lines


def _guide(record: dict[str, Any]) -> str:
    major_minor = ".".join(record["version"].split(".")[:2])
    lines = ["---", "status: ACTIVE", f"version: {major_minor}", "owner_skill: ls-github-publishing-workflow", "---", "", f"# LocalSetup {record['version']}", "", record["summary"], "", "## Highlights", ""]
    lines.extend(f"- {row['text']}" for row in record["highlights"])
    lines.append("")
    lines.extend([f"After publication, see the [release and downloads](https://github.com/CruxExperts/localsetup/releases/tag/v{record['version']}) for release assets.", ""])
    lines.extend(_section("Compatibility", record["compatibility"]))
    lines.extend(_section("Update", record["update"]))
    lines.extend(["For installation and source refresh, read the [quickstart update instructions](../QUICKSTART.md#update). Adapter changes follow the [adapter ownership guide](../ADAPTER_OWNERSHIP.md).", ""])
    lines.extend(_section("Verify the download", record["verification"]))
    lines.extend([
        "Download the framework archive with both its `.sha256` checksum and `.cdx.json` SBOM sidecars. Keep all three files in the same directory before running `verify-release`.",
        "",
        "```bash",
        f"uv run --locked python ls/tools/localsetup.py --source-root . verify-release /path/to/localsetup-v{record['version']}.tar.gz",
        "```",
        "",
    ])
    return "\n".join(lines)


def notes(record: dict[str, Any]) -> str:
    """Render deterministic GitHub release-note prose from a validated record."""
    record = validate_record(record)
    lines = [f"## LocalSetup {record['version']}", "", record["summary"], "", "### Highlights", ""]
    lines.extend(f"- {row['text']}" for row in record["highlights"])
    for title, key in (("Compatibility", "compatibility"), ("Update", "update"), ("Verification", "verification")):
        lines.extend(["", f"### {title}", ""])
        for value in record[key]:
            lines.extend([value, ""])
    guide = f"https://github.com/CruxExperts/localsetup/blob/v{record['version']}/ls/docs/releases/{record['version']}.md"
    lines.extend(["", f"Download the archive with its `.sha256` checksum and `.cdx.json` SBOM sidecars, then follow the [release guide]({guide}).", ""])
    return "\n".join(lines)


def render_outputs(repo_root: Path, record: dict[str, Any]) -> dict[str, str]:
    """Return complete managed-document candidates without writing any files."""
    root = Path(repo_root)
    record = validate_record(record)
    replacements = {
        "README.md": _summary_block(record),
        "ls/README.md": _link_block(record, guide=f"docs/releases/{record['version']}.md"),
        "ls/docs/README.md": _link_block(record, guide=f"releases/{record['version']}.md"),
    }
    rendered: dict[str, str] = {}
    for path, block in _BLOCKS.items():
        rendered[path] = _replace_block((root / path).read_text(encoding="utf-8"), block, replacements[path])
    rendered[f"ls/docs/releases/{record['version']}.md"] = _guide(record)
    return rendered
