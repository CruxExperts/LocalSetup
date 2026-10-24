"""Immutable local carrier adapter for the existing publication protocol."""
import hashlib
import math
import os
from pathlib import Path
import time

from .container_storage import ensure_private_directory
from .container_transfer import TransferError, _check
from .envelope_io import AnchoredFile, ExclusiveOutput


class LocalDirectoryPublisher:
    """Map opaque carrier keys to flat names; never overwrite existing bytes."""
    def __init__(self, root: Path):
        ensure_private_directory(root, create=False)
        self.root = root
        info = root.stat()
        self.identity = info.st_dev, info.st_ino

    def _operation(self, key, timeout_seconds, cancel_check):
        if (type(key) is not str or not 1 <= len(key.encode()) <= 4096 or '\0' in key
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 3600 or not callable(cancel_check)):
            raise TransferError('limits_invalid')
        deadline = time.monotonic()+timeout_seconds
        def check():
            _check(cancel_check, deadline)
            ensure_private_directory(self.root, create=False)
            info = self.root.stat()
            if (info.st_dev, info.st_ino) != self.identity:
                raise TransferError('destination_changed')
        check()
        return self.root/(hashlib.sha256(key.encode()).hexdigest()+'.lssc'), check

    def observe(self, key, *, timeout_seconds, cancel_check):
        path, check = self._operation(key, timeout_seconds, cancel_check)
        try:
            with AnchoredFile(path, private=True) as source:
                digest, offset = hashlib.sha256(), 0
                while offset < source.size:
                    check()
                    block = os.pread(source.file, min(65536, source.size-offset), offset)
                    if not block:
                        raise TransferError('destination_changed')
                    offset += len(block)
                    digest.update(block)
                check()
                if not source.unchanged():
                    raise TransferError('destination_changed')
                return source.size, digest.hexdigest()
        except FileNotFoundError:
            check()
            return None

    def create_if_absent(self, key, ciphertext, *, length, sha256, timeout_seconds, cancel_check):
        from .artifact_records import validate_digest
        validate_digest(sha256)
        if type(length) is not int or length <= 0:
            raise TransferError('limits_invalid')
        path, check = self._operation(key, timeout_seconds, cancel_check)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            check()
            return 'exists'
        try:
            with ExclusiveOutput(path) as output:
                name, descriptor = output.stage()
                with os.fdopen(descriptor, 'wb') as stream:
                    digest, remaining = hashlib.sha256(), length
                    while remaining:
                        check()
                        data = ciphertext.read(min(65536, remaining))
                        if type(data) is not bytes or not data or len(data) > remaining:
                            raise TransferError('ciphertext_invalid')
                        stream.write(data)
                        digest.update(data)
                        remaining -= len(data)
                    if ciphertext.read(1) or digest.hexdigest() != sha256:
                        raise TransferError('ciphertext_invalid')
                    stream.flush()
                    os.fsync(stream.fileno())
                check()
                output.publish(name)
                # Publication is immutable even if a later check/acknowledgement
                # fails. Existing publication-job recovery observes exact bytes.
                output.published_identity = None
                check()
                return 'created'
        except FileExistsError:
            check()
            return 'exists'

    def put(self, key, ciphertext, **options):
        deadline = time.monotonic()+options['timeout_seconds']
        result = self.create_if_absent(key, ciphertext, **options)
        if result == 'exists':
            _check(options['cancel_check'], deadline)
            found = self.observe(key, timeout_seconds=deadline-time.monotonic(),
                                 cancel_check=options['cancel_check'])
            if found != (options['length'], options['sha256']):
                raise TransferError('ciphertext_collision')
