from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ls.core.git_subprocess import run_git
from ls.core.provenance import base_provenance, json_with_provenance

from .constants import LOCAL_DOC_EXCLUDES, PUBLIC_DOCS

def _rel(repo_root: Path, path: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _artifact_id(repo_root: Path, path: Path) -> Path:
    try:
        return Path(_rel(repo_root, path))
    except ValueError:
        return path


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_json(path: Path, payload: dict[str, Any], *, repo_root: Path | None = None, emitter: str = "docs-align") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = payload
    if repo_root is not None:
        output = json_with_provenance(
            payload,
            base_provenance(
                repo_root,
                emitter=emitter,
                artifact_path=_artifact_id(repo_root, path),
                generated_commit_parent=True,
            ),
        )
    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    data = yaml.safe_load(text[4:end])
    return data if isinstance(data, dict) else {}


def _markdown_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    candidates: list[Path]
    if (repo_root / ".git").exists():
        completed = run_git(
            repo_root,
            ["ls-files", "--cached", "-z", "--", "*.md"],
            text=False,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            candidates = [
                repo_root / raw.decode("utf-8", errors="replace")
                for raw in completed.stdout.split(b"\0")
                if raw
            ]
        else:
            candidates = sorted(repo_root.rglob("*.md"))
    else:
        candidates = sorted(repo_root.rglob("*.md"))

    for path in candidates:
        if not path.is_file():
            continue
        rel_path = path.relative_to(repo_root)
        if any(part in LOCAL_DOC_EXCLUDES for part in rel_path.parts) or rel_path.is_relative_to(".agents/state"):
            continue
        files.append(path)
    return sorted(files)


def _classify_doc(repo_root: Path, path: Path) -> str:
    rel = _rel(repo_root, path)
    if rel.startswith("ls/docs/_generated/"):
        return "generated"
    if rel.startswith("ls/docs/"):
        return "framework"
    if rel in PUBLIC_DOCS or rel.startswith("docs/"):
        return "public"
    if "/SKILL.md" in rel:
        return "skill"
    return "other"


def _managed_blocks(text: str) -> list[str]:
    return sorted(set(re.findall(r"<!--\s*([A-Za-z0-9_.-]+):start\s*-->", text)))
def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_external(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) or target.startswith("#")


def _resolve_markdown_target(repo_root: Path, source: Path, target: str) -> Path | None:
    clean = target.split("#", 1)[0].strip()
    if not clean or _is_external(clean):
        return None
    clean = clean.replace("%20", " ")
    if clean.startswith("/"):
        candidates = [(repo_root / clean.lstrip("/")).resolve()]
    else:
        candidates = [(source.parent / clean).resolve()]
    for candidate in candidates:
        try:
            candidate.relative_to(repo_root)
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    for candidate in candidates:
        try:
            candidate.relative_to(repo_root)
            return candidate
        except ValueError:
            continue
    return None


def _markdown_links(text: str) -> Iterable[tuple[str, str, int, str]]:
    for match in re.finditer(r"!\[([^\]]*)\]\(([^)]+)\)", text):
        yield "image", match.group(2).strip(), match.start(), match.group(1).strip()
    for match in re.finditer(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)", text):
        yield "link", match.group(2).strip(), match.start(), match.group(1).strip()
    for match in re.finditer(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.IGNORECASE):
        alt = re.search(r"\balt=[\"']([^\"']*)[\"']", match.group(0), flags=re.IGNORECASE)
        yield "image", match.group(1).strip(), match.start(), alt.group(1).strip() if alt else ""
    for match in re.finditer(r"<source\b[^>]*\bsrcset=[\"']([^\"']+)[\"'][^>]*>", text, flags=re.IGNORECASE):
        for candidate in match.group(1).split(","):
            parts = candidate.strip().split()
            if parts:
                yield "source", parts[0], match.start(), ""
