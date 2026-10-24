"""Explicit create-only acquisition of an authenticated public runtime release."""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
import subprocess
import time

from . import release_manifest
from .runtime_install import _write_json


def download(tag: str, trusted_sha256: str, destination: Path, *, timeout: float = 300) -> dict:
    if not isinstance(tag, str) or not tag.startswith('v'):
        raise ValueError('An explicit canonical release tag is required')
    expected_version = release_manifest.version(tag[1:])
    if not isinstance(trusted_sha256, str) or not release_manifest.native_bundle.DIGEST.fullmatch(trusted_sha256):
        raise ValueError('An independently trusted release manifest SHA-256 is required')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Release download timeout must be finite and positive')
    if shutil.which('gh') is None:
        raise RuntimeError('GitHub CLI is required for release download; use manually acquired verified assets for offline setup')
    destination = destination.absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('Release downloads require a fresh directory; inspect retained files instead of replaying')
    destination.mkdir(mode=0o700, parents=True)
    receipt = {'schema_version': 1, 'status': 'incomplete', 'tag': tag,
               'release_manifest_sha256': trusted_sha256}
    _write_json(destination / 'status.json', receipt)
    deadline = time.monotonic() + timeout

    def fetch(names: list[str]) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Release download deadline expired; inspect retained files')
        command = ['gh', 'release', 'download', tag, '--repo', 'CruxExperts/LocalSetup',
                   '--dir', str(destination)]
        for name in names:
            command.extend(['--pattern', name])
        try:
            subprocess.run(command, check=True, timeout=remaining, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, 'GH_PROMPT_DISABLED': '1'})
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Release download failed; inspect retained files and GitHub CLI availability') from exc

    fetch([release_manifest.NAME])
    manifest = destination / release_manifest.NAME
    value = release_manifest.load(manifest, trusted_sha256)
    if value['version'] != expected_version:
        raise ValueError('Authenticated manifest version differs from the requested release tag')
    fetch(sorted(value['files']))
    resolved = release_manifest.resolve(manifest, trusted_sha256)
    if time.monotonic() >= deadline:
        raise TimeoutError('Release download deadline expired; inspect retained files')
    result = {**receipt, 'status': 'downloaded', 'manifest': str(manifest),
              'source_commit': resolved['release']['source_commit'], 'files': value['files']}
    _write_json(destination / 'status.json', result)
    return result
