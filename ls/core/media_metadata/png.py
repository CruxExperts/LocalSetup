"""Pixel-preserving PNG copies with an explicit metadata allowlist."""
from __future__ import annotations

import io
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
FIELDS = frozenset({"Copyright", "Author", "Website", "Usage Terms", "Identity Version"})


def encode_clean_png(image, metadata: Mapping[str, str]) -> bytes:
    """Recreate pixels, dropping EXIF/XMP/ICC/comments and all inherited info."""
    from PIL import Image, PngImagePlugin

    if not metadata or set(metadata) - FIELDS:
        raise ValueError("PNG metadata must use the supported rights fields")
    if any(not isinstance(value, str) or not value.strip() or len(value) > 4096
           or any(ord(char) < 32 for char in value) for value in metadata.values()):
        raise ValueError("PNG metadata fields must contain bounded printable text")
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("Image exceeds the supported pixel budget")
    if getattr(image, "is_animated", False):
        raise ValueError("Animated PNG is unsupported; preserve the original")
    # Keep orientation-dependent and color-profile-dependent media out of this
    # lossless metadata-only path rather than silently changing its appearance.
    image.load()
    if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
        raise ValueError("High-depth images require explicit normalization")
    if image.getexif().get(274, 1) != 1 or any(
        key in image.info for key in ("icc_profile", "gamma", "chromaticity", "cicp")
    ):
        raise ValueError("Image needs orientation/color normalization before metadata cleanup")
    rgba = image.convert("RGBA")
    clean = Image.frombytes("RGBA", rgba.size, rgba.tobytes())
    tags = PngImagePlugin.PngInfo()
    for key, value in sorted(metadata.items()):
        tags.add_text(key, value)
    output = io.BytesIO()
    clean.save(output, format="PNG", pnginfo=tags, optimize=True)
    result = output.getvalue()
    with Image.open(io.BytesIO(result)) as verified:
        verified.load()
        if verified.getexif() or verified.info != dict(metadata):
            raise ValueError("PNG metadata readback does not match the requested policy")
        if verified.convert("RGBA").tobytes() != rgba.tobytes():
            raise ValueError("PNG pixel readback differs from the source")
    return result


def prepare_png(source: Path, metadata: Mapping[str, str], *, replace_copyright: bool = False) -> bytes:
    from PIL import Image

    if any(path.is_symlink() for path in (source, *source.parents)):
        raise ValueError("Input must not use symlink aliases")
    descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Input must be a regular PNG within the byte budget")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Input exceeds the byte budget")
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24 and data[24] > 8:
        raise ValueError("High-depth PNG requires explicit normalization")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        position = 8
        while position < len(data):
            if position + 12 > len(data):
                raise ValueError("Truncated PNG chunk")
            length = int.from_bytes(data[position:position + 4], "big")
            end = position + length + 12
            if end > len(data):
                raise ValueError("Truncated PNG chunk payload")
            if data[position + 4:position + 8] in {b"cICP", b"gAMA", b"cHRM", b"iCCP"}:
                raise ValueError("PNG color metadata requires explicit normalization")
            position = end
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError("Only PNG is supported by this automatic cleanup path")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("Image exceeds the supported pixel budget")
        if getattr(image, "is_animated", False):
            raise ValueError("Animated PNG is unsupported; preserve the original")
        image.load()  # Text after IDAT is legal; inspect it before attribution decisions.
        existing = [image.info.get("Copyright"), image.getexif().get(33432)]
        if any(value and value != metadata.get("Copyright") for value in existing) and not replace_copyright:
            raise ValueError("Existing copyright differs; review attribution before explicit replacement")
        authors = [image.info.get("Author"), image.getexif().get(315)]
        if any(value and value != metadata.get("Author") for value in authors) and not replace_copyright:
            raise ValueError("Existing creator differs; review attribution before explicit replacement")
        if any("xmp" in key.lower() or "license" in key.lower() for key in image.info) and not replace_copyright:
            raise ValueError("Embedded rights carrier requires attribution review before explicit replacement")
        return encode_clean_png(image, metadata)


def write_copy(destination: Path, data: bytes) -> None:
    """Publish a verified copy without overwriting a source or existing output."""
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError("Output must not use symlink aliases")
    if not destination.parent.is_dir():
        raise ValueError("Output directory must already exist")
    descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
