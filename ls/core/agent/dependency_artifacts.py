"""Shared locked dependency acquisition recipe and explicit private preparation."""
from __future__ import annotations

import hashlib
from email.parser import BytesParser
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

from ..sdk_payload.artifacts import inspect_artifact
from . import release_manifest, runtime_install

LOCKS = ('sdk-build.lock', 'sdk-runtime.lock')


def environment(directory: Path) -> dict[str, str]:
    result = {key: value for key, value in os.environ.items()
              if key in {'PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'TMPDIR'}}
    result.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1',
                  PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CACHE_DIR=str(directory / 'pip-cache'),
                  UV_CACHE_DIR=str(directory / 'uv-cache'), UV_PYTHON_DOWNLOADS='never')
    return result


def recipe(directory: Path, python: str) -> list[dict]:
    """Keep download order, hash policy and source-metadata isolation in one owner."""
    pip = [python, '-I', '-B', '-m', 'pip']
    wheelhouse = str(directory / 'wheelhouse')
    build = str(directory / LOCKS[0])
    runtime = str(directory / LOCKS[1])
    private_python = str(directory / 'download-env/bin/python')
    download = ['download', '--disable-pip-version-check', '--require-hashes', '--no-deps',
                '--no-build-isolation', '--dest', wheelhouse]
    return [
        {'stage': 'build-artifact-download', 'lock': LOCKS[0], 'timeout': 300,
         'argv': [*pip, *download, '--only-binary', ':all:', '-r', build]},
        {'stage': 'private-build-environment', 'lock': LOCKS[0], 'timeout': 60,
         'argv': ['uv', '--no-config', 'venv', '--offline', '--python', python, str(directory / 'download-env')]},
        {'stage': 'locked-build-backend', 'lock': LOCKS[0], 'timeout': 120,
         'argv': [*pip, '--python', private_python, 'install', '--no-index', '--find-links', wheelhouse,
                  '--require-hashes', '--only-binary', ':all:', '--no-deps', '-r', build]},
        {'stage': 'runtime-artifact-download', 'lock': LOCKS[1], 'timeout': 300,
         'argv': [*pip, '--python', private_python, *download, '-r', runtime]},
    ]


def wheel_exports(wheel: Path, trusted_sha256: str) -> dict[str, bytes]:
    if not isinstance(trusted_sha256, str) or not runtime_install.DIGEST.fullmatch(trusted_sha256):
        raise ValueError('An independently trusted framework wheel SHA-256 is required')
    raw = release_manifest.read_file(wheel, runtime_install.MAX_WHEEL_BYTES)
    if hashlib.sha256(raw).hexdigest() != trusted_sha256:
        raise ValueError('Framework wheel digest mismatch')
    inspect_artifact(wheel)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        metadata = [item for item in archive.infolist() if item.filename.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or metadata[0].file_size > 1024 * 1024:
            raise ValueError('Framework wheel metadata is missing, ambiguous or oversized')
        identity = BytesParser().parsebytes(archive.read(metadata[0]))
        if identity.get_all('Name') != ['localsetup'] or len(identity.get_all('Version', [])) != 1:
            raise ValueError('Expected framework distribution metadata')
        release_manifest.version(identity['Version'])
        wanted = [*LOCKS, 'sdk-dependency-receipt.json']
        members = archive.namelist()
        for name in wanted:
            member = 'ls/config/' + name
            if members.count(member) != 1 or archive.getinfo(member).file_size > 2 * 1024 * 1024:
                raise ValueError('Dependency exports are missing, ambiguous or oversized')
        exports = {name: archive.read('ls/config/' + name) for name in LOCKS}
        receipt = json.loads(archive.read('ls/config/sdk-dependency-receipt.json'),
                             object_pairs_hook=release_manifest.native_bundle._unique)
    if (not isinstance(receipt, dict) or set(receipt) != {'schema_version', 'source_sha256', 'files'}
            or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1
            or not isinstance(receipt['source_sha256'], str)
            or not runtime_install.DIGEST.fullmatch(receipt['source_sha256'])
            or receipt['files'] != {name: hashlib.sha256(data).hexdigest() for name, data in exports.items()}):
        raise ValueError('Dependency export receipt does not match the authenticated wheel')
    if release_manifest.read_file(wheel, runtime_install.MAX_WHEEL_BYTES) != raw:
        raise ValueError('Framework wheel changed during dependency inspection')
    return exports


def prepare(wheel: Path, trusted_sha256: str, destination: Path, *, python: str | None = None,
            timeout: float = 300) -> dict:
    """Fetch hash-locked artifacts and bootstrap their build tools, never select a runtime."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Dependency preparation timeout must be finite and positive')
    destination = destination.absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('Dependency preparation requires a fresh directory; inspect retained state instead of replaying')
    exports = wheel_exports(wheel.absolute(), trusted_sha256)
    python = python or sys.executable
    if shutil.which('uv') is None:
        raise RuntimeError('Dependency preparation requires the uv CLI')
    env = environment(destination)
    deadline = time.monotonic() + timeout
    try:
        probe = subprocess.run([python, '-I', '-B', '-m', 'pip', '--version'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, timeout=min(10, timeout))
        if probe.returncode:
            raise RuntimeError('pip unavailable')
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Choose --python with pip available; no pip installation or interpreter fallback was attempted') from exc
    destination.mkdir(mode=0o700, parents=True)
    receipt = {'schema_version': 1, 'status': 'incomplete', 'wheel_sha256': trusted_sha256,
               'lock_sha256': {name: hashlib.sha256(data).hexdigest() for name, data in exports.items()}}
    runtime_install._write_json(destination / 'status.json', receipt)
    for name, data in exports.items():
        (destination / name).write_bytes(data)
    (destination / 'wheelhouse').mkdir(mode=0o700)
    for step in recipe(destination, python):
        runtime_install._write_json(destination / 'status.json', {**receipt, 'stage': step['stage']})
        try:
            runtime_install._run(step['argv'], directory=destination,
                                 deadline=min(deadline, time.monotonic() + step['timeout']), environment=env)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise RuntimeError('Dependency preparation failed during ' + step['stage'] + '; incomplete directory retained') from exc
    allowed = set(re.findall(rb'--hash=sha256:([0-9a-f]{64})', b'\n'.join(exports.values())))
    inventory = {}
    for artifact in sorted((destination / 'wheelhouse').iterdir()):
        digest = hashlib.sha256(release_manifest.read_file(artifact, runtime_install.MAX_WHEEL_BYTES)).hexdigest()
        if digest.encode() not in allowed:
            raise ValueError('Dependency artifact is not authenticated by the selected locks')
        inventory[artifact.name] = digest
    if not inventory or time.monotonic() >= deadline:
        raise RuntimeError('Dependency preparation is empty or exceeded its deadline; incomplete directory retained')
    for name, data in exports.items():
        if release_manifest.read_file(destination / name, 2 * 1024 * 1024) != data:
            raise ValueError('Dependency lock changed during preparation')
    result = {**receipt, 'status': 'prepared', 'wheelhouse': str(destination / 'wheelhouse'), 'artifacts': inventory}
    runtime_install._write_json(destination / 'status.json', result)
    return result
