#!/usr/bin/env python3
"""Render the README graphics from the approved LocalSetup visual tokens."""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import subprocess
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image
from ls.core.media_metadata.png import encode_clean_png

ROOT = Path(__file__).resolve().parents[3]
BRAND = ROOT / "assets" / "brand"
PLATES = BRAND / "plates"
FONT_HASHES = {
    "JetBrainsMono-Regular.ttf": "a0bf60ef0f83c5ed4d7a75d45838548b1f6873372dfac88f71804491898d138f",
    "JetBrainsMono-SemiBold.ttf": "1b3bfa1ed5665a4ce3f9feb68d2d4e40e70bf8b4b7d9a3edd418f321b4e166a0",
}
RIGHTS = "Copyright © 2026 Crux Experts LLC"
NOTICE = (
    "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, "
    "distribution, or publication without prior written permission is prohibited."
)


def _text(x: int, y: int, value: str, *, size: int | float = 20, weight: int = 400,
          fill: str, anchor: str = "start", letter_spacing: int | float | None = None) -> str:
    spacing = "" if letter_spacing is None else f' letter-spacing="{letter_spacing}"'
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" '
            f'fill="{html.escape(fill, quote=True)}" text-anchor="{anchor}"{spacing}>'
            f'{html.escape(value)}</text>')


def _diagram_frame(width: int, height: int, title: str, content: str,
                   theme: dict[str, str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title, quote=True)}">'
        f'<metadata>{RIGHTS}. https://www.cruxexperts.com/. {NOTICE}</metadata>'
        f'<rect width="{width}" height="{height}" fill="{theme["bg"]}"/>'
        f'<path d="M0 {height - 42} H{width}" stroke="{theme["border"]}" opacity=".4"/>'
        f'<g font-family="JetBrains Mono" font-variant-ligatures="none">{content}</g></svg>'
    )


def _rasterize(svg: str, svg2png: Callable[..., bytes | None]) -> Image.Image:
    data = svg2png(bytestring=svg.encode("utf-8"))
    if data is None:
        raise RuntimeError("SVG renderer returned no image bytes")
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGBA")


def _brand_header(theme: dict[str, str], width: int) -> str:
    accent, text, muted = theme["accent"], theme["text"], theme["muted"]
    return (
        f'<text x="56" y="66" font-size="28" font-weight="600" fill="{text}">'
        f'<tspan fill="{accent}">[</tspan>LS<tspan fill="{accent}">]</tspan></text>'
        + _text(158, 66, "LocalSetup", size=28, weight=600, fill=text)
        + _text(width - 56, 64, "REPO-LOCAL / CLI-FIRST", size=13, fill=muted, anchor="end")
    )


def _plate(name: str, size: tuple[int, int]) -> Image.Image:
    """Export a reviewed full composition without cropping or changing its content."""
    with Image.open(PLATES / name) as source:
        if abs(source.width / source.height - size[0] / size[1]) > .01:
            raise RuntimeError(f"Plate aspect ratio does not match export: {name}")
        return source.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


def _card(theme: dict[str, str], x: int, y: int, width: int, number: str, title: str,
          lines: tuple[str, ...], indicator: str) -> str:
    accent, text, muted = theme["accent"], theme["text"], theme["muted"]
    height = 250
    content = (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" '
        f'fill="{theme["surface"]}" stroke="{theme["border"]}" stroke-width="1.5"/>'
        f'<circle cx="{x + width - 25}" cy="{y + 29}" r="6" fill="{indicator}"/>'
        + _text(x + 23, y + 44, f"[{number}]", size=15, weight=600, fill=accent)
        + _text(x + 23, y + 89, title, size=19, weight=600, fill=text)
        + f'<path d="M{x + 23} {y + 109} H{x + width - 23}" '
          f'stroke="{theme["border"]}" stroke-width="1"/>'
    )
    for index, line in enumerate(lines):
        content += _text(x + 23, y + 148 + index * 34, line, size=15, fill=text)
    content += _text(x + 23, y + height - 19, "TERMINAL", size=10, fill=muted,
                     letter_spacing=1.4)
    return content


def _card_row(theme: dict[str, str],
              cards: tuple[tuple[str, str, tuple[str, ...]], ...],
              y_positions: tuple[int, ...]) -> str:
    xs = (56, 354, 652, 950)
    width = 274
    height = 250
    content = ""
    for index in range(len(cards) - 1):
        x1 = xs[index] + width
        x2 = xs[index + 1]
        y1 = y_positions[index] + height // 2
        y2 = y_positions[index + 1] + height // 2
        path = f"M{x1} {y1} C{x1 + 8} {y1}, {x2 - 8} {y2}, {x2} {y2}"
        content += (
            f'<path d="{path}" fill="none" stroke="{theme["accent"]}" '
            'stroke-opacity=".34" stroke-width="8"/>'
            f'<path d="{path}" fill="none" stroke="{theme["accent"]}" '
            'stroke-width="2.5"/>'
            f'<path d="M{x2 - 9} {y2 - 6} L{x2} {y2} L{x2 - 9} {y2 + 6}" '
            'fill="none" stroke="#A9D2FF" stroke-width="2.5" stroke-linecap="round" '
            'stroke-linejoin="round"/>'
        )
    indicators = ("#168EFF", "#18D5B2", "#A66CFF", "#F2A541")
    for index, (number, title, lines) in enumerate(cards):
        content += _card(theme, xs[index], y_positions[index], width, number, title,
                         lines, indicators[index])
    return content


def _architecture(theme: dict[str, str]) -> str:
    content = _brand_header(theme, 1280)
    content += _text(56, 128, "One source. Selected packages.", size=32, weight=600,
                     fill=theme["accent"])
    content += _text(56, 166, "Keep framework source separate from project adapters.",
                     size=16, fill=theme["text"])
    cards = (
        ("01", "Source checkout", ("Skills + workflows", "Docs + templates")),
        ("02", "LocalSetup CLI", ("Resolve selection", "Plan + apply")),
        ("03", "Managed library", ("Selected packages", "Required dependencies")),
        ("04", "Project adapters", ("Selected agent hosts", "Links or portable copies")),
    )
    content += _card_row(theme, cards, (222, 248, 222, 248))
    content += (
        '<rect x="56" y="566" width="1168" height="62" rx="10" '
        f'fill="{theme["surface"]}" fill-opacity=".92" stroke="{theme["border"]}"/>'
    )
    content += _text(80, 605, "Verification  •  lock metadata  •  rollback records",
                     size=18, fill=theme["text"])
    content += _text(56, 691, "Custom project skills stay in place.",
                     size=18, fill=theme["accent"])
    return _diagram_frame(1280, 740, "LocalSetup architecture", content, theme)


def _lifecycle(theme: dict[str, str]) -> str:
    content = _brand_header(theme, 1280)
    content += _text(56, 128, "Inspect. Plan. Apply. Verify.", size=32, weight=600,
                     fill=theme["accent"])
    content += _text(56, 166, "A deliberate install, with a path back.",
                     size=16, fill=theme["text"])
    cards = (
        ("01", "Inspect", ("doctor + context", "Understand the target")),
        ("02", "Plan", ("Review selected", "changes")),
        ("03", "Apply", ("install", "Write managed packages")),
        ("04", "Verify", ("verify", "Check managed state")),
    )
    content += _card_row(theme, cards, (222, 248, 222, 248))
    content += (
        '<rect x="455" y="546" width="370" height="38" rx="19" '
        f'fill="{theme["surface"]}" stroke="{theme["accent"]}" stroke-opacity=".6"/>'
    )
    content += _text(640, 571, "Confirm scope before apply", size=15, fill=theme["accent"],
                     anchor="middle")
    content += (
        '<rect x="56" y="606" width="1168" height="62" rx="10" '
        f'fill="{theme["surface"]}" fill-opacity=".92" stroke="{theme["border"]}"/>'
    )
    content += _text(80, 645, "Rollback restores recorded managed paths.",
                     size=18, fill=theme["text"])
    content += _text(56, 722, "Updates use the same ownership boundaries.",
                     size=18, fill=theme["muted"])
    return _diagram_frame(1280, 760, "LocalSetup install lifecycle", content, theme)


def _font_config(path: Path) -> None:
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
        '<fontconfig><include>/etc/fonts/fonts.conf</include>'
        f'<dir>{html.escape(str(BRAND / "fonts"))}</dir></fontconfig>',
        encoding="utf-8",
    )


def render(*, check: bool, social_output: Path | None, pack_output: Path | None = None) -> None:
    for name, expected in FONT_HASHES.items():
        if hashlib.sha256((BRAND / "fonts" / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Font hash mismatch: {name}")
    import cairosvg

    tokens = json.loads((BRAND / "tokens.json").read_text(encoding="utf-8"))
    provenance = json.loads((PLATES / "provenance.json").read_text(encoding="utf-8"))
    for source in provenance["assets"]:
        path = ROOT / source["path"]
        if path.parent != PLATES or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise RuntimeError("Reviewed brand plate provenance differs")
    # Committed generation plates are the source for marketing imagery. The
    # exporter only resamples to delivery dimensions and attaches rights metadata.
    plate_specs = (
        ("localsetup-readme-hero.png", "hero-light.png", (2048, 768)),
        ("localsetup-readme-hero-dark.png", "hero-dark.png", (2048, 768)),
        ("localsetup-social-preview.png", "social-landscape.png", (1280, 640)),
        ("localsetup-social-square.png", "social-square.png", (1080, 1080)),
        ("localsetup-social-wide.png", "social-wide.png", (1536, 512)),
        ("localsetup-mark.png", "mark.png", (512, 512)),
    )
    outputs = [(ROOT / "assets" / name, _plate(plate, size)) for name, plate, size in plate_specs]
    with tempfile.TemporaryDirectory(prefix="brand-fontconfig-") as temporary:
        config = Path(temporary) / "fonts.conf"
        _font_config(config)
        os.environ["FONTCONFIG_FILE"] = str(config)
        for weight, style in (("regular", "Regular"), ("demibold", "SemiBold")):
            resolved = subprocess.run(
                ["fc-match", "-f", "%{family}|%{style}|%{file}", f"JetBrains Mono:weight={weight}"],
                check=True, capture_output=True, text=True,
            ).stdout
            if "JetBrains Mono" not in resolved or f"JetBrainsMono-{style}.ttf" not in resolved:
                raise RuntimeError(f"Font resolution failed for weight {weight}: {resolved}")
        for mode, suffix in (("light", ""), ("dark", "-dark")):
            theme = tokens["theme"][mode]
            outputs.extend((
                (ROOT / "assets" / f"localsetup-architecture{suffix}.png",
                 _rasterize(_architecture(theme), cairosvg.svg2png)),
                (ROOT / "assets" / f"localsetup-install-lifecycle{suffix}.png",
                 _rasterize(_lifecycle(theme), cairosvg.svg2png)),
            ))
        package: dict[str, bytes] = {}
        manifest = {"schema_version": 1, "version": tokens["version"],
                    "copyright": RIGHTS, "website": "https://www.cruxexperts.com/",
                    "usage_terms": NOTICE, "assets": []}
        for path, image in outputs:
            result = encode_clean_png(image, {
                "Copyright": RIGHTS, "Author": "Crux Experts LLC",
                "Website": "https://www.cruxexperts.com/", "Usage Terms": NOTICE,
                "Identity Version": tokens["version"],
            })
            if check:
                if not path.exists() or path.read_bytes() != result:
                    raise RuntimeError(f"Graphic differs from generated output: {path}")
            else:
                path.write_bytes(result)
            digest = hashlib.sha256(result).hexdigest()
            manifest["assets"].append({"path": path.relative_to(ROOT).as_posix(),
                                       "width": image.width, "height": image.height,
                                       "sha256": digest, "bytes": len(result)})
            package[path.name] = result
            if path.name == "localsetup-social-preview.png" and social_output is not None:
                if check:
                    if not social_output.exists() or social_output.read_bytes() != result:
                        raise RuntimeError(f"Social output differs: {social_output}")
                else:
                    social_output.parent.mkdir(parents=True, exist_ok=True)
                    social_output.write_bytes(result)
            print(f"{path}: {digest}")
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        manifest_path = BRAND / "manifest.json"
        if check:
            if not manifest_path.exists() or manifest_path.read_bytes() != manifest_bytes:
                raise RuntimeError("Brand manifest differs from generated output")
        else:
            manifest_path.write_bytes(manifest_bytes)
        package["manifest.json"] = manifest_bytes
        for name in ("ASSET_LICENSE.md", "tokens.json", "PROMPTS.md"):
            package[name] = (BRAND / name).read_bytes()
        if pack_output is not None:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(package.items()):
                    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
            data = buffer.getvalue()
            if check:
                if not pack_output.exists() or pack_output.read_bytes() != data:
                    raise RuntimeError(f"Brand pack differs: {pack_output}")
            else:
                pack_output.parent.mkdir(parents=True, exist_ok=True)
                pack_output.write_bytes(data)
            print(f"{pack_output}: {hashlib.sha256(data).hexdigest()}")


def main(argv=None, *, source_root=None) -> int:
    global ROOT, BRAND, PLATES
    if source_root is not None:
        ROOT = Path(source_root).resolve()
        BRAND = ROOT / "assets" / "brand"
        PLATES = BRAND / "plates"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed graphics without writing")
    parser.add_argument("--social-output", type=Path,
                        help="also write the social preview to an additional path")
    parser.add_argument("--pack-output", type=Path, help="write a deterministic versioned asset ZIP")
    args = parser.parse_args(argv)
    render(check=args.check, social_output=args.social_output, pack_output=args.pack_output)
    return 0


if __name__ == "__main__":
    main()
