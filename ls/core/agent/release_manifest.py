"""Resolve one independently authenticated runtime release to offline setup inputs."""
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from ..versioning_models import SemVer
from . import native_bundle

NAME = 'localsetup-runtime.json'
MAX_MANIFEST = 64 * 1024
MAX_WHEEL = 256 * 1024 * 1024


def read_file(path: Path, limit: int) -> bytes:
    """Bound reads and reject special files without opening a blocking FIFO."""
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Release asset parents must not be symlinks')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Release asset must be a bounded regular file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if not raw or len(raw) > limit:
            raise ValueError('Release asset is empty or exceeds its size limit')
        return raw
    finally:
        os.close(descriptor)


def version(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,8}\.\d{1,8}\.\d{1,8}', value):
        raise ValueError('Invalid runtime release version')
    if str(SemVer.parse(value)) != value:
        raise ValueError('Runtime release version must be canonical')
    return value


def load(path: Path, trusted_sha256: str) -> dict:
    if not isinstance(trusted_sha256, str) or not native_bundle.DIGEST.fullmatch(trusted_sha256):
        raise ValueError('An independently trusted release manifest SHA-256 is required')
    raw = read_file(path, MAX_MANIFEST)
    if hashlib.sha256(raw).hexdigest() != trusted_sha256:
        raise ValueError('Release manifest digest mismatch')
    value = json.loads(raw, object_pairs_hook=native_bundle._unique)
    keys = {'schema_version', 'version', 'source_commit', 'wheel', 'wheel_sha256',
            'native_bundle', 'native_sha256', 'native_origin', 'files'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ValueError('Unsupported runtime release manifest')
    release_version = version(value['version'])
    if not isinstance(value['source_commit'], str) or not re.fullmatch(r'[0-9a-f]{40}', value['source_commit']):
        raise ValueError('Invalid runtime release source commit')
    files = value['files']
    if not isinstance(files, dict) or len(files) != 8:
        raise ValueError('Runtime release manifest must describe the complete eight-file payload')
    for name, digest in files.items():
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                or name in {NAME, 'status.json', 'runtime-SHA256SUMS'}
                or not isinstance(digest, str) or not native_bundle.DIGEST.fullmatch(digest)):
            raise ValueError('Invalid runtime release filename or digest')
    wheel, bundle = value['wheel'], value['native_bundle']
    if (wheel != f'localsetup-{release_version}-py3-none-any.whl'
            or not isinstance(bundle, str) or bundle == wheel or bundle not in files
            or wheel not in files or files[wheel] != value['wheel_sha256']
            or files[bundle] != value['native_sha256']):
        raise ValueError('Runtime release artifact roles or digests disagree')
    origin = value['native_origin']
    if (not isinstance(origin, dict) or set(origin) != {'repository', 'tag'}
            or origin['repository'] != 'CruxExperts/LocalSetup'
            or not isinstance(origin['tag'], str) or not origin['tag'].startswith('v')):
        raise ValueError('Invalid native release origin')
    version(origin['tag'][1:])
    return value


def resolve(path: Path, trusted_sha256: str, *, sdk_only: bool = False) -> dict:
    """Verify the complete local set; never fetch, execute, or create runtime state."""
    path = path.absolute()
    value = load(path, trusted_sha256)
    wheel_raw = None
    for name, digest in value['files'].items():
        raw = read_file(path.parent / name, MAX_WHEEL if name == value['wheel'] else native_bundle.MAX_BYTES)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Release asset digest mismatch: ' + name)
        if name == value['wheel']:
            wheel_raw = raw
    # Check metadata in the authenticated snapshot, before any installer effects.
    with zipfile.ZipFile(io.BytesIO(wheel_raw)) as archive:
        metadata = [item for item in archive.infolist() if item.filename.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or metadata[0].file_size > 1024 * 1024:
            raise ValueError('Release wheel metadata is ambiguous or oversized')
        identity = BytesParser().parsebytes(archive.read(metadata[0]))
        if identity.get_all('Name') != ['localsetup'] or identity.get_all('Version') != [value['version']]:
            raise ValueError('Release wheel metadata differs from its authenticated manifest')
    bundle = path.parent / value['native_bundle']
    native_bundle.read_static(bundle, value['native_sha256'])
    return {'wheel': path.parent / value['wheel'], 'sha256': value['wheel_sha256'],
            'native_inputs': {} if sdk_only else {'sandbox_bundle': bundle, 'sandbox_sha256': value['native_sha256']},
            'release': {'release_manifest_sha256': trusted_sha256, 'source_commit': value['source_commit'],
                        'release_version': value['version'], 'sdk_only': sdk_only}}
