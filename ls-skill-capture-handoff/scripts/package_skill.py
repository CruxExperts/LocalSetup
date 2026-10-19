#!/usr/bin/env python3
"""Create a deterministic public source archive from an explicit reviewed file list."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import os
import stat
import tarfile

ALLOWED = {'.py', '.md', '.json', '.yaml', '.yml', '.txt', '.in', '.example'}
PRIVATE = {'state', 'logs', '.git', '.env', '.agents', '.ssh', '.gnupg', '__pycache__'}


def reviewed_files(root: Path, paths: list[str]) -> dict[str, bytes]:
    result = {}
    root = root.resolve(strict=True)
    if not paths or len(paths) > 256:
        raise ValueError('require 1..256 explicitly reviewed paths')
    for value in paths:
        relative = PurePosixPath(value)
        if (relative.is_absolute() or '\\' in value or '..' in relative.parts
                or value != relative.as_posix() or value in result
                or any(part in PRIVATE or part.startswith('.') for part in relative.parts)
                or not relative.parts or relative.name == 'MANIFEST.json'):
            raise ValueError('unsafe, private or duplicate archive path')
        if relative.suffix not in ALLOWED and relative.name != 'LICENSE':
            raise ValueError('file type is not approved for public source')
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('symlinks are forbidden in public source archives')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError('require bounded regular source files')
        data = path.read_bytes()
        data.decode('utf-8')
        if (b'-----BEGIN ' + b'PGP PRIVATE KEY') in data or (b'-----BEGIN ' + b'OPENSSH PRIVATE KEY') in data:
            raise ValueError('private key material is forbidden')
        result[value] = data
    if sum(map(len, result.values())) > 10_000_000:
        raise ValueError('source archive exceeds size limit')
    return result


def archive_bytes(files: dict[str, bytes]) -> bytes:
    manifest = {'schema_version': 1, 'files': [
        {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())]}
    payload = dict(files)
    payload['MANIFEST.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    output = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(payload.items()):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o644
                item.mtime = item.uid = item.gid = 0
                archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--files', type=Path, required=True,
                        help='JSON array of reviewed root-relative source files')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    paths = json.loads(args.files.read_text())
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        parser.error('file list must be a JSON array of strings')
    try:
        data = archive_bytes(reviewed_files(args.root, paths))
        # Exclusive creation prevents silently replacing an unrelated artifact.
        with args.output.open('xb') as stream:
            os.chmod(args.output, 0o600)
            stream.write(data)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.exit(1, type(exc).__name__ + ': archive preparation failed\n')
    print(json.dumps({'sha256': hashlib.sha256(data).hexdigest(),
                      'bytes': len(data), 'source_files': len(paths)}))


if __name__ == '__main__':
    main()
