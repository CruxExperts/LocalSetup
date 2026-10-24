"""Check runtime wheel contents against the exact clean release checkout."""
from __future__ import annotations

from email.parser import BytesParser
import configparser
import hashlib
import io
from pathlib import Path
import subprocess
import stat
import tomllib
import zipfile

from ..sdk_payload.artifacts import inspect_artifact
from ..sdk_payload.sbom import SBOM_PATH
from ..versioning import read_version
from .runtime_install import _wheel_bytes


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def checked_wheel(root: Path, wheel: Path) -> tuple[bytes, dict]:
    """Reject stale, foreign, or dirty source; inspect without importing the wheel."""
    source = git(root, 'rev-parse', 'HEAD')
    if git(root, 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Runtime release packaging requires a clean source checkout')
    version = str(read_version(root))
    if wheel.name != f'localsetup-{version}-py3-none-any.whl':
        raise ValueError('Runtime wheel filename differs from the release version')
    raw = _wheel_bytes(wheel)
    inspected = inspect_artifact(wheel)
    expected_sdk = hashlib.sha256((root / 'vendor/lscli/manifest.json').read_bytes()).hexdigest()
    if inspected['manifest_sha256'] != expected_sdk:
        raise ValueError('Runtime wheel SDK differs from the release source')
    tracked = set(git(root, 'ls-files', 'ls').splitlines())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Runtime wheel has duplicate members')
        if (sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024
                or any(item.file_size > 16 * 1024 * 1024 or item.flag_bits & 1
                       or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                       for item in archive.infolist())):
            raise ValueError('Runtime wheel has oversized or nonregular members')
        metadata = [name for name in names if name.endswith('.dist-info/METADATA')]
        if len(metadata) != 1 or archive.getinfo(metadata[0]).file_size > 1024 * 1024:
            raise ValueError('Runtime wheel metadata is ambiguous or oversized')
        message = BytesParser().parsebytes(archive.read(metadata[0]))
        if message.get_all('Name') != ['localsetup'] or message.get_all('Version') != [version]:
            raise ValueError('Runtime wheel metadata differs from the release version')
        metadata_root = metadata[0].rsplit('/', 1)[0] + '/'
        if metadata_root != f'localsetup-{version}.dist-info/':
            raise ValueError('Runtime wheel metadata directory differs from the release version')
        if any(not name.startswith(('ls/', metadata_root)) for name in names):
            raise ValueError('Runtime wheel contains an unexpected package namespace')
        project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
        if (sorted(message.get_all('Requires-Dist', [])) != sorted(project.get('dependencies', []))
                or message.get_all('Requires-Python') != [project['requires-python']]
                or message.get_all('Provides-Extra')):
            raise ValueError('Runtime wheel dependency metadata differs from the release source')
        entrypoints = configparser.ConfigParser(interpolation=None)
        entrypoints.optionxform = str
        try:
            entrypoints.read_string(archive.read(metadata_root + 'entry_points.txt').decode('utf-8'))
        except (configparser.Error, KeyError, UnicodeError) as exc:
            raise ValueError('Runtime wheel entry points are invalid') from exc
        if (entrypoints.defaults() or entrypoints.sections() != ['console_scripts']
                or dict(entrypoints['console_scripts']) != project['scripts']):
            raise ValueError('Runtime wheel entry points differ from the release source')
        wheel_metadata = BytesParser().parsebytes(archive.read(metadata_root + 'WHEEL'))
        if (wheel_metadata.get_all('Root-Is-Purelib') != ['true']
                or wheel_metadata.get_all('Tag') != ['py3-none-any']):
            raise ValueError('Runtime wheel layout differs from its supported platform tag')
        verified = set()
        for name in names:
            if not name.startswith('ls/') or name.endswith('/'):
                continue
            if name.startswith('ls/_sdk_payload/') or name == SBOM_PATH:
                continue
            if name not in tracked or (root / name).is_symlink() or archive.read(name) != (root / name).read_bytes():
                raise ValueError('Runtime wheel content differs from the release source')
            verified.add(name)
        required = {name for name in tracked if name.endswith('.py')}
        if not required <= verified:
            raise ValueError('Runtime wheel omits release Python modules')
    if _wheel_bytes(wheel) != raw:
        raise ValueError('Runtime wheel changed during inspection')
    if git(root, 'rev-parse', 'HEAD') != source or git(root, 'status', '--porcelain'):
        raise ValueError('Runtime release source changed during inspection')
    return raw, {'version': version, 'source_commit': source,
                 'wheel': wheel.name, 'wheel_sha256': hashlib.sha256(raw).hexdigest()}
