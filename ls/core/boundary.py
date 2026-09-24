from __future__ import annotations

import fnmatch
import tarfile
from pathlib import Path

PRIVATE_MARKERS = (
    ".cache",
    ".agents/state",
    ".localsetup-maint",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "_internal_docs",
    "private",
)

# This shipped Python module defines references and a resolver, not stored secrets.
PUBLIC_SECRET_NAMED_SOURCE = "ls/core/openpgp/secrets.py"


def scan_tar_for_leaks(artifact_path: Path, private_paths: list[str], patterns: list[str] | None = None) -> list[str]:
    if not artifact_path.exists():
        return [f"missing artifact: {artifact_path}"]

    normalized_private = tuple(path.rstrip("/") for path in private_paths)
    glob_patterns = patterns or [".env", "*.key", "*.pem", "*.secret", "*.secret.*", "secrets.*"]
    leaks: list[str] = []

    with tarfile.open(artifact_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.strip("/")
            parts = name.split("/")
            top = parts[0] if parts else name
            if top in normalized_private or name in normalized_private:
                leaks.append(name)
                continue
            if any(marker in name.lower() for marker in PRIVATE_MARKERS):
                leaks.append(name)
                continue
            default_source_exception = patterns is None and name == PUBLIC_SECRET_NAMED_SOURCE
            if (member.isfile() and not default_source_exception
                    and any(fnmatch.fnmatch(Path(name).name.lower(), pattern.lower()) for pattern in glob_patterns)):
                leaks.append(name)

    return sorted(set(leaks))
