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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "assets" / "brand"
FONT_HASHES = {
    "JetBrainsMono-Regular.ttf": "a0bf60ef0f83c5ed4d7a75d45838548b1f6873372dfac88f71804491898d138f",
    "JetBrainsMono-SemiBold.ttf": "1b3bfa1ed5665a4ce3f9feb68d2d4e40e70bf8b4b7d9a3edd418f321b4e166a0",
}
RIGHTS = "Copyright © 2026 Crux Experts LLC"
NOTICE = (
    "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, "
    "distribution, or publication without prior written permission is prohibited."
)


def _text(x: int, y: int, value: str, *, size: int = 20, weight: int = 400,
          fill: str, anchor: str = "start") -> str:
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}">{html.escape(value)}</text>')


def _frame(width: int, height: int, theme: dict, title: str, content: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title, quote=True)}">'
            f'<metadata>{RIGHTS}. https://www.cruxexperts.com/. {NOTICE}</metadata>'
            f'<rect width="{width}" height="{height}" fill="{theme["bg"]}"/>'
            f'<g font-family="JetBrains Mono" font-variant-ligatures="none">{content}</g></svg>')


def _brand_header(theme: dict, width: int) -> str:
    accent, text, muted = theme["accent"], theme["text"], theme["muted"]
    return (f'<text x="56" y="76" font-size="30" font-weight="600" fill="{accent}">[LS]</text>'
            f'<text x="168" y="76" font-size="30" font-weight="600" fill="{text}">'
            f'<tspan fill="{accent}">L</tspan>ocal<tspan fill="{accent}">S</tspan>etup</text>'
            + _text(width - 56, 74, "REPO-LOCAL / CLI-FIRST", size=15, fill=muted, anchor="end"))


def _hero(theme: dict, *, social: bool = False) -> str:
    width, height = 1280, 640 if social else 400
    accent, text, muted = theme["accent"], theme["text"], theme["muted"]
    title_y = 272 if social else 192
    rule_y = height - 93
    content = _brand_header(theme, width)
    content += _text(56, title_y, "Own your stack.", size=76 if social else 62, weight=600, fill=accent)
    content += _text(56, title_y + 55, "A repo-local operating layer for coding agents.", size=24, fill=text)
    if social:
        content += _text(56, title_y + 103, "Skills. Context. Control.", size=22, fill=muted)
    content += f'<path d="M56 {rule_y} H1224" stroke="{theme["border"]}"/>'
    for x, label in ((56, "[01] READ"), (446, "[02] REVIEW"), (866, "[03] APPLY")):
        content += _text(x, height - 46, label, size=18, fill=text)
    return _frame(width, height, theme, "LocalSetup: Own your stack", content)


def _card(theme: dict, x: int, y: int, width: int, number: str, title: str,
          lines: tuple[str, ...]) -> str:
    content = (f'<rect x="{x}" y="{y}" width="{width}" height="286" rx="10" '
               f'fill="{theme["surface"]}" stroke="{theme["border"]}" stroke-width="2"/>')
    content += _text(x + 24, y + 48, f"[{number}]", size=22, weight=600, fill=theme["accent"])
    content += _text(x + 24, y + 98, title, size=23, weight=600, fill=theme["text"])
    content += f'<path d="M{x + 24} {y + 122} H{x + width - 24}" stroke="{theme["border"]}"/>'
    for index, line in enumerate(lines):
        content += _text(x + 24, y + 164 + index * 34, line, size=17, fill=theme["text"])
    return content


def _architecture(theme: dict) -> str:
    content = _brand_header(theme, 1280)
    content += _text(56, 157, "One source. Selected packages.", size=39, weight=600, fill=theme["accent"])
    content += _text(56, 197, "Keep framework source separate from project adapters.", size=20, fill=theme["text"])
    cards = (
        ("01", "Source checkout", ("Skills + workflows", "Docs + templates")),
        ("02", "LocalSetup CLI", ("Resolve selection", "Plan + apply")),
        ("03", "Managed library", ("Selected packages", "Required dependencies")),
        ("04", "Project adapters", ("Selected agent hosts", "Links or portable copies")),
    )
    for index, (number, title, lines) in enumerate(cards):
        x = 56 + index * 306
        content += _card(theme, x, 255, 274, number, title, lines)
        if index < 3:
            content += _text(x + 288, 414, "→", size=30, fill=theme["accent"])
    content += (f'<rect x="56" y="574" width="1168" height="62" rx="8" '
                f'fill="{theme["raised"]}" stroke="{theme["border"]}"/>')
    content += _text(80, 614, "Verification  •  lock metadata  •  rollback records", size=19, fill=theme["text"])
    content += _text(56, 692, "Custom project skills stay in place.", size=19, fill=theme["accent"])
    return _frame(1280, 740, theme, "LocalSetup architecture", content)


def _lifecycle(theme: dict) -> str:
    content = _brand_header(theme, 1280)
    content += _text(56, 157, "Inspect. Plan. Apply. Verify.", size=39, weight=600, fill=theme["accent"])
    content += _text(56, 197, "A deliberate install, with a path back.", size=20, fill=theme["text"])
    cards = (
        ("01", "Inspect", ("doctor + context", "Understand the target")),
        ("02", "Plan", ("Review selected", "changes")),
        ("03", "Apply", ("install", "Write managed packages")),
        ("04", "Verify", ("verify", "Check managed state")),
    )
    for index, (number, title, lines) in enumerate(cards):
        x = 56 + index * 306
        content += _card(theme, x, 255, 274, number, title, lines)
        if index < 3:
            content += _text(x + 288, 414, "→", size=30, fill=theme["accent"])
    content += _text(513, 566, "Confirm scope before apply", size=17, fill=theme["accent"])
    content += (f'<rect x="56" y="604" width="1168" height="62" rx="8" '
                f'fill="{theme["raised"]}" stroke="{theme["border"]}"/>')
    content += _text(80, 644, "Rollback restores recorded managed paths.", size=19, fill=theme["text"])
    content += _text(56, 716, "Updates use the same ownership boundaries.", size=19, fill=theme["muted"])
    return _frame(1280, 760, theme, "LocalSetup install lifecycle", content)


def _font_config(path: Path) -> None:
    path.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd">'
        '<fontconfig><include>/etc/fonts/fonts.conf</include>'
        f'<dir>{html.escape(str(BRAND / "fonts"))}</dir></fontconfig>',
        encoding="utf-8",
    )


def render(*, check: bool, social_output: Path | None) -> None:
    for name, expected in FONT_HASHES.items():
        actual = hashlib.sha256((BRAND / "fonts" / name).read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError(f"Font hash mismatch: {name}")
    import cairosvg
    from PIL import Image, PngImagePlugin

    tokens = json.loads((BRAND / "tokens.json").read_text(encoding="utf-8"))
    outputs: list[tuple[Path, str]] = []
    for mode, theme in tokens["theme"].items():
        outputs.extend([
            (ROOT / "assets" / ("localsetup-readme-hero.png" if mode == "light" else "localsetup-readme-hero-dark.png"), _hero(theme)),
            (ROOT / "assets" / ("localsetup-architecture.png" if mode == "light" else "localsetup-architecture-dark.png"), _architecture(theme)),
            (ROOT / "assets" / ("localsetup-install-lifecycle.png" if mode == "light" else "localsetup-install-lifecycle-dark.png"), _lifecycle(theme)),
        ])
        if mode == "dark" and social_output is not None:
            outputs.append((social_output, _hero(theme, social=True)))
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
        for path, svg in outputs:
            data = cairosvg.svg2png(bytestring=svg.encode("utf-8"))
            image = Image.open(io.BytesIO(data))
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("Copyright", RIGHTS)
            metadata.add_text("Website", "https://www.cruxexperts.com/")
            metadata.add_text("Usage Terms", NOTICE)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", pnginfo=metadata, optimize=False)
            result = buffer.getvalue()
            if check:
                if not path.exists() or path.read_bytes() != result:
                    raise RuntimeError(f"Graphic differs from generated output: {path}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result)
            print(f"{path}: {hashlib.sha256(result).hexdigest()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify committed graphics without writing")
    parser.add_argument("--social-output", type=Path, help="write the dark GitHub preview to private maintenance state")
    args = parser.parse_args()
    render(check=args.check, social_output=args.social_output)


if __name__ == "__main__":
    main()
