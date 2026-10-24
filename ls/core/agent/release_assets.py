"""Assemble a runtime wheel and the complete pinned native redistribution set."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from . import native_bundle, native_sbom
from .release_wheel import checked_wheel, git

MANIFEST = 'localsetup-runtime.json'
CHECKSUMS = 'runtime-SHA256SUMS'


def _read(path: Path, maximum: int = native_bundle.MAX_BYTES) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('Runtime release input must be a regular file without symlinks')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if not raw or len(raw) > maximum:
        raise ValueError('Runtime release input exceeds its size limit or is empty')
    return raw


def native_policy(root: Path) -> dict:
    value = json.loads(_read(root / 'ls/config/native-release.json', 16384),
                       object_pairs_hook=native_bundle._unique)
    keys = {'schema_version', 'repository', 'tag', 'bundle', 'sbom', 'provenance', 'files'}
    if (not isinstance(value, dict) or set(value) != keys
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['repository'] != 'CruxExperts/LocalSetup'
            or not isinstance(value['tag'], str) or not re.fullmatch(r'v\d+\.\d+\.\d+', value['tag'])
            or not isinstance(value['files'], dict) or len(value['files']) != 7):
        raise ValueError('Invalid native release policy')
    for name, digest in value['files'].items():
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                or not isinstance(digest, str) or not native_bundle.DIGEST.fullmatch(digest)):
            raise ValueError('Invalid native release asset name or digest')
    roles = [value[name] for name in ('bundle', 'sbom', 'provenance')]
    if any(not isinstance(name, str) or name not in value['files'] for name in roles) or len(set(roles)) != 3:
        raise ValueError('Native release asset roles are ambiguous')
    if set(value['files']) & {MANIFEST, CHECKSUMS}:
        raise ValueError('Native release asset name is reserved')
    return value


def native_assets(directory: Path, policy: dict) -> dict[str, bytes]:
    payloads = {name: _read(directory / name) for name in policy['files']}
    for name, expected in policy['files'].items():
        if hashlib.sha256(payloads[name]).hexdigest() != expected:
            raise ValueError('Pinned native release asset digest mismatch: ' + name)
    # Inspect the exact authenticated snapshots, not mutable download paths.
    with tempfile.TemporaryDirectory(prefix='localsetup-native-check-') as temp:
        stage = Path(temp)
        for name, raw in payloads.items():
            (stage / name).write_bytes(raw)
        digest = policy['files'][policy['bundle']]
        contents = native_bundle.read_static(stage / policy['bundle'], digest)
        native_sbom.verify(stage / policy['bundle'], digest, stage / policy['sbom'])
    provenance = json.loads(payloads[policy['provenance']], object_pairs_hook=native_bundle._unique)
    identities = {'bundle_sha256': digest, 'sbom_sha256': policy['files'][policy['sbom']],
                  'executable_sha256': hashlib.sha256(contents['bwrap']).hexdigest()}
    if (not isinstance(provenance, dict) or provenance.get('artifacts') != identities
            or provenance.get('target') != native_bundle.TARGET):
        raise ValueError('Native provenance differs from the pinned bundle')
    sources = provenance.get('libcap_corresponding_source', [])
    if (not isinstance(sources, list) or len(sources) != 3
            or any(not isinstance(item, dict) or not isinstance(item.get('name'), str)
                   or item['name'] not in policy['files']
                   or policy['files'][item['name']] != item.get('sha256') for item in sources)):
        raise ValueError('Native redistribution is missing corresponding libcap source')
    source_name = f"bubblewrap-{native_bundle.COMPONENTS['bubblewrap']['version']}.tar.xz"
    if policy['files'].get(source_name) != native_bundle.COMPONENTS['bubblewrap']['source_sha256']:
        raise ValueError('Native redistribution is missing the supported bubblewrap source')
    expected = {policy['bundle'], policy['sbom'], policy['provenance'], source_name,
                *(item['name'] for item in sources)}
    if set(payloads) != expected:
        raise ValueError('Native redistribution inventory differs from its provenance')
    return payloads


def download_native(policy: dict) -> dict[str, bytes]:
    """Fetch exactly the pinned set; validate all bytes before returning any payload."""
    with tempfile.TemporaryDirectory(prefix='localsetup-native-download-') as temp:
        directory = Path(temp)
        command = ['gh', 'release', 'download', policy['tag'], '--repo', policy['repository'],
                   '--dir', str(directory)]
        for name in sorted(policy['files']):
            command.extend(['--pattern', name])
        subprocess.run(command, check=True, timeout=120, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return native_assets(directory, policy)


def prepare(root: Path, wheel: Path, output: Path, *, native_directory: Path | None = None) -> dict:
    """Create a release-only artifact set; never install, select, or execute its code."""
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError('Runtime output must be a fresh directory; inspect retained state instead of replaying')
    wheel_bytes, identity = checked_wheel(root, wheel)
    policy = native_policy(root)
    payloads = download_native(policy) if native_directory is None else native_assets(native_directory, policy)
    if git(root, 'rev-parse', 'HEAD') != identity['source_commit'] or git(root, 'status', '--porcelain'):
        raise ValueError('Runtime release source changed during assembly')
    payloads[wheel.name] = wheel_bytes
    manifest = {'schema_version': 1, **identity, 'native_bundle': policy['bundle'],
                'native_sha256': policy['files'][policy['bundle']],
                'native_origin': {'repository': policy['repository'], 'tag': policy['tag']},
                'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(payloads.items())}}
    payloads[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    payloads[CHECKSUMS] = ''.join(f'{hashlib.sha256(raw).hexdigest()}  {name}\n'
                                for name, raw in sorted(payloads.items())).encode()
    output.mkdir(mode=0o700, parents=True)
    for name, raw in payloads.items():
        with (output / name).open('xb') as stream:
            stream.write(raw)
        if _read(output / name, max(len(raw), native_bundle.MAX_BYTES)) != raw:
            raise ValueError('Runtime release output changed during writing')
    return {'ok': True, **identity, 'native_sha256': manifest['native_sha256'],
            'assets': sorted(payloads), 'manifest_sha256': hashlib.sha256(payloads[MANIFEST]).hexdigest()}
