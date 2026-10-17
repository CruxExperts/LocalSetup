#!/usr/bin/env python3
"""Render the README graphics from the approved LocalSetup visual tokens."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, PngImagePlugin

ROOT = Path(__file__).resolve().parents[2]
BRAND = ROOT / "assets" / "brand"
BANNER_REFERENCE = BRAND / "banner-reference.png"
SPACE_BACKGROUND = BRAND / "space-background.png"
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
                   background_data: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title, quote=True)}">'
        f'<metadata>{RIGHTS}. https://www.cruxexperts.com/. {NOTICE}</metadata>'
        f'<defs><linearGradient id="scene-shade" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#030A19" stop-opacity=".58"/>'
        '<stop offset=".56" stop-color="#030A19" stop-opacity=".66"/>'
        '<stop offset="1" stop-color="#030A19" stop-opacity=".78"/>'
        '</linearGradient></defs>'
        f'<image href="data:image/png;base64,{background_data}" width="{width}" height="{height}" '
        'preserveAspectRatio="xMidYMid slice"/>'
        f'<rect width="{width}" height="{height}" fill="url(#scene-shade)"/>'
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


def _hero_overlay(theme: dict[str, str]) -> str:
    accent, muted = theme["accent"], theme["muted"]
    content = (
        '<g font-family="JetBrains Mono" font-variant-ligatures="none">'
        f'<text x="410" y="352" font-size="24" font-weight="400" letter-spacing="6.5" '
        f'fill="{muted}">AGENT FRAMEWORK</text>'
        + _text(108, 479, "Your rules.", size=108, weight=600, fill=accent)
        + f'<path d="M110 511 H199" stroke="{accent}" stroke-width="5"/>'
        + '</g>'
    )
    # The plate retains its original wordmark, slogan, value icons, and agent panels.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="2056" height="765" '
        'viewBox="0 0 2056 765" role="img" aria-label="LocalSetup: Your rules">'
        f'<metadata>{RIGHTS}. https://www.cruxexperts.com/. {NOTICE}</metadata>'
        f'{content}</svg>'
    )


def _hero(reference: Image.Image, theme: dict[str, str],
          svg2png: Callable[..., bytes | None]) -> Image.Image:
    if reference.size != (2056, 765):
        raise RuntimeError(
            f"Banner reference must be 2056x765; found {reference.width}x{reference.height}"
        )
    image = reference.convert("RGBA")
    overlay = _rasterize(_hero_overlay(theme), svg2png)
    image.alpha_composite(overlay)
    return image


def _social_preview(hero: Image.Image, background: Image.Image) -> Image.Image:
    width, height = 1280, 640
    canvas = ImageOps.fit(
        background.convert("RGBA"), (width, height), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.67),
    )
    scale = min(width / hero.width, height / hero.height)
    resized = hero.resize(
        (round(hero.width * scale), round(hero.height * scale)),
        Image.Resampling.LANCZOS,
    )
    y = (height - resized.height) // 2
    fade = 18
    mask = Image.new("L", resized.size, 255)
    draw = ImageDraw.Draw(mask)
    for offset in range(fade):
        alpha = round(255 * offset / (fade - 1))
        draw.line((0, offset, resized.width, offset), fill=alpha)
        draw.line((0, resized.height - 1 - offset, resized.width, resized.height - 1 - offset),
                  fill=alpha)
    canvas.paste(resized, (0, y), mask)
    return canvas


def _mark(reference: Image.Image, background: Image.Image) -> Image.Image:
    """Reuse the actual reference lettering rather than approximating its typeface."""
    canvas = ImageOps.fit(background.convert("RGBA"), (512, 512),
                          method=Image.Resampling.LANCZOS)
    mark = reference.crop((104, 190, 360, 342)).convert("RGB")
    red, green, blue = mark.split()
    brightness = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    # Remove the dark source matte while retaining the blue and white lettering.
    alpha = brightness.point([min(255, max(0, (v - 40) * 255 // 180))
                              for v in range(256)])
    mark = mark.convert("RGBA")
    mark.putalpha(alpha)
    mark = mark.resize((360, 214), Image.Resampling.LANCZOS)
    canvas.alpha_composite(mark, (76, 149))
    return canvas


def _card(theme: dict[str, str], x: int, y: int, width: int, number: str, title: str,
          lines: tuple[str, ...], indicator: str) -> str:
    accent, text, muted = theme["accent"], theme["text"], theme["muted"]
    height = 250
    content = (
        f'<rect x="{x + 3}" y="{y + 7}" width="{width}" height="{height}" rx="12" '
        'fill="#020713" fill-opacity=".72"/>'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" '
        f'fill="#071328" fill-opacity=".93" stroke="{accent}" stroke-opacity=".35" stroke-width="6"/>'
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="12" '
        f'fill="#071328" fill-opacity=".94" stroke="#397BC5" stroke-opacity=".78" stroke-width="1.5"/>'
        f'<path d="M{x + 13} {y + 2} H{x + width - 13}" stroke="#73B7FF" '
        'stroke-opacity=".7" stroke-width="2"/>'
        f'<circle cx="{x + width - 25}" cy="{y + 29}" r="6" fill="{indicator}"/>'
        + _text(x + 23, y + 44, f"[{number}]", size=15, weight=600, fill=accent,
                letter_spacing=1)
        + _text(x + 23, y + 89, title, size=19, weight=600, fill=text)
        + f'<path d="M{x + 23} {y + 109} H{x + width - 23}" '
          f'stroke="{theme["border"]}" stroke-width="1.2"/>'
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


def _architecture(theme: dict[str, str], background_data: str) -> str:
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
    return _diagram_frame(1280, 740, "LocalSetup architecture", content, background_data)


def _lifecycle(theme: dict[str, str], background_data: str) -> str:
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
    return _diagram_frame(1280, 760, "LocalSetup install lifecycle", content, background_data)


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

    tokens = json.loads((BRAND / "tokens.json").read_text(encoding="utf-8"))
    theme = tokens["theme"]["dark"]
    with Image.open(BANNER_REFERENCE) as source:
        reference = source.convert("RGBA")
    with Image.open(SPACE_BACKGROUND) as source:
        background = source.convert("RGBA")
    if background.size != (1536, 1024):
        raise RuntimeError(
            f"Space background must be 1536x1024; found {background.width}x{background.height}"
        )
    background_data = base64.b64encode(SPACE_BACKGROUND.read_bytes()).decode("ascii")
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
        hero = _hero(reference, theme, cairosvg.svg2png)
        architecture = _rasterize(_architecture(theme, background_data), cairosvg.svg2png)
        lifecycle = _rasterize(_lifecycle(theme, background_data), cairosvg.svg2png)
        mark = _mark(reference, background)
        social = _social_preview(hero, background)
        social_path = ROOT / "assets" / "localsetup-social-preview.png"
        outputs = [
            (ROOT / "assets" / "localsetup-readme-hero.png", hero),
            (ROOT / "assets" / "localsetup-readme-hero-dark.png", hero),
            (ROOT / "assets" / "localsetup-architecture.png", architecture),
            (ROOT / "assets" / "localsetup-architecture-dark.png", architecture),
            (ROOT / "assets" / "localsetup-install-lifecycle.png", lifecycle),
            (ROOT / "assets" / "localsetup-install-lifecycle-dark.png", lifecycle),
            (social_path, social),
            (ROOT / "assets" / "localsetup-mark.png", mark),
        ]
        if social_output is not None and social_output.resolve() != social_path.resolve():
            outputs.append((social_output, social))
        for path, image in outputs:
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
    parser.add_argument("--social-output", type=Path,
                        help="also write the social preview to an additional path")
    args = parser.parse_args()
    render(check=args.check, social_output=args.social_output)


if __name__ == "__main__":
    main()
