from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

from .constants import ASSETS_README, SCHEMA_VERSION
from .io import _markdown_files, _read_text, _rel, _resolve_markdown_target, _markdown_links

BRAND_GRAPHICS = {
    "assets/localsetup-readme-hero.png",
    "assets/localsetup-readme-hero-dark.png",
    "assets/localsetup-architecture.png",
    "assets/localsetup-architecture-dark.png",
    "assets/localsetup-install-lifecycle.png",
    "assets/localsetup-install-lifecycle-dark.png",
}

def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _svg_dimensions(path: Path) -> tuple[int, int] | None:
    text = _read_text(path)[:2048]
    viewbox = re.search(r'viewBox=["\']\s*[-0-9.]+\s+[-0-9.]+\s+([0-9.]+)\s+([0-9.]+)\s*["\']', text)
    if viewbox:
        return int(float(viewbox.group(1))), int(float(viewbox.group(2)))
    width = re.search(r'width=["\']([0-9.]+)', text)
    height = re.search(r'height=["\']([0-9.]+)', text)
    if width and height:
        return int(float(width.group(1))), int(float(height.group(1)))
    return None


def collect_asset_manifest(repo_root: Path) -> dict[str, Any]:
    assets = []
    assets_root = repo_root / "assets"
    if assets_root.is_dir():
        for path in sorted(assets_root.rglob("*")):
            if not path.is_file():
                continue
            rel = _rel(repo_root, path)
            suffix = path.suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
                continue
            dims = _png_dimensions(path) if suffix == ".png" else _svg_dimensions(path) if suffix == ".svg" else None
            references = []
            for md in _markdown_files(repo_root):
                text = _read_text(md)
                for kind, target, _, _ in _markdown_links(text):
                    resolved = _resolve_markdown_target(repo_root, md, target) if kind in {"image", "source"} else None
                    if resolved and resolved == path.resolve():
                        references.append(_rel(repo_root, md))
                        break
            assets.append(
                {
                    "path": rel,
                    "type": suffix.lstrip(".") or "unknown",
                    "dimensions": {"width": dims[0], "height": dims[1]} if dims else None,
                    "references": sorted(set(references)),
                    "provenance": "repository-maintained asset",
                    "license": (
                        "Crux Experts LLC private asset terms; see [visual asset rights](brand/ASSET_LICENSE.md)"
                        if rel in BRAND_GRAPHICS else "Repository license unless otherwise documented"
                    ),
                    "alt_text_required": True,
                }
            )
    return {"schema_version": SCHEMA_VERSION, "assets": assets, "count": len(assets)}
def write_assets_readme(repo_root: Path, manifest: dict[str, Any], *, dry_run: bool) -> bool:
    text = build_assets_readme_text(manifest)
    path = repo_root / ASSETS_README
    before = _read_text(path) if path.exists() else ""
    if before == text:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return True


def build_assets_readme_text(manifest: dict[str, Any]) -> str:
    lines = [
        "# Asset Inventory",
        "",
        "This file is maintained by `ls/tools/docs_alignment.py apply --scope assets` and the generated-doc refresh.",
        "",
        "| Asset | Type | Dimensions | References | Notes |",
        "|---|---|---|---|---|",
    ]
    for asset in manifest["assets"]:
        dims = asset["dimensions"]
        dim_text = f"{dims['width']}x{dims['height']}" if dims else "unknown"
        refs = ", ".join(f"`{ref}`" for ref in asset["references"]) or "none found"
        lines.append(f"| `{asset['path']}` | {asset['type']} | {dim_text} | {refs} | {asset['license']} |")
    lines.append("")
    return "\n".join(lines) + "\n"
