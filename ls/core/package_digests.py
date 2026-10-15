from __future__ import annotations

import hashlib
from pathlib import Path


FRAMED_PACKAGE_DIGEST_ALGORITHM = "sha256-framed-files-v1"
_FRAMED_DOMAIN = b"localsetup.package-digest\x00sha256-framed-files-v1\x00"
_EXCLUDED_MARKERS = {".localsetup-managed.json", ".localsetup-managed"}


def package_digests(path: Path) -> tuple[str | None, str | None]:
    """Return the historical and unambiguous package digests for one tree."""
    if not path.exists():
        return None, None

    files = []
    for child in path.rglob("*"):
        if not child.is_file() or child.is_symlink():
            continue
        relative_path = child.relative_to(path)
        if (
            (len(relative_path.parts) == 1 and relative_path.name in _EXCLUDED_MARKERS)
            or "__pycache__" in relative_path.parts
            or relative_path.suffix in {".pyc", ".pyo"}
        ):
            continue
        files.append((relative_path, child))

    files.sort(key=lambda item: item[0])
    if not files:
        return None, None

    legacy = hashlib.sha256()
    framed = hashlib.sha256()
    framed.update(_FRAMED_DOMAIN)
    framed.update(len(files).to_bytes(8, "big"))
    for relative_path, child in files:
        relative_bytes = relative_path.as_posix().encode("utf-8")
        content = child.read_bytes()

        # Keep the old persisted calculation exactly intact for older markers
        # and registry writers that still expect its concatenated byte stream.
        legacy.update(relative_bytes)
        legacy.update(b"\0")
        legacy.update(content)

        # Length-prefix both fields so path/content boundaries are explicit.
        framed.update(len(relative_bytes).to_bytes(8, "big"))
        framed.update(relative_bytes)
        framed.update(len(content).to_bytes(8, "big"))
        framed.update(content)

    return legacy.hexdigest(), framed.hexdigest()


def legacy_package_digest(path: Path) -> str | None:
    return package_digests(path)[0]


def framed_package_digest(path: Path) -> str | None:
    return package_digests(path)[1]
