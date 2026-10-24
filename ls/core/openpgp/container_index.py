"""Normal indexed references and bounded, private catalog metadata pages."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import tempfile
import time

from .artifact_records import canonical_bytes, validate_opaque_id
from .container_reader import ContainerReadError, VerifiedContainerRead, _inspect_output
from .envelope_io import AnchoredFile


def indexed_reference(name):
    return isinstance(name, str) and (name == '@artifacts' or name.startswith('@artifacts/'))


def parse_reference(name):
    parts = name.split('/')
    if parts[0] != '@artifacts' or len(parts) not in (1, 2, 3, 5):
        raise ContainerReadError('unavailable')
    try:
        if len(parts) > 1:
            validate_opaque_id(parts[1])
        if len(parts) > 2:
            validate_opaque_id(parts[2])
        if len(parts) == 5:
            if parts[3] != '@files':
                raise ValueError
            validate_opaque_id(parts[4])
    except ValueError:
        raise ContainerReadError('unavailable') from None
    return (parts[1] if len(parts)>1 else None, parts[2] if len(parts)>2 else None,
            parts[4] if len(parts)==5 else None)


def stage_index(reader, rows, *, budget, check, deadline):
    """Reuse normal verified-file paging for an authenticated catalog projection."""
    stack = ExitStack()
    try:
        binding = reader.check()
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix='.lssc-read-', dir=reader.policy.boundary)))
        output = directory/'index'
        total, expected = 0, hashlib.sha256()
        with output.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            for row in rows:
                budget.checkpoint()
                data = canonical_bytes(row)+b'\n'
                total += len(data)
                if total > budget.limits.file_limits.max_plaintext_bytes:
                    raise ContainerReadError('limit_exceeded')
                budget.reserve_temp(len(data))
                stream.write(data)
                expected.update(data)
        opened = stack.enter_context(AnchoredFile(output, private=True))
        digest, text, size = _inspect_output(opened, budget)
        if (digest, size) != (expected.hexdigest(), total):
            raise ContainerReadError('changed')
        result = VerifiedContainerRead(stack, reader, None, opened, (), digest, size, text,
            min(deadline, time.monotonic()+reader.cache_seconds), binding)
        result.check(check)
        return result
    except BaseException:
        stack.close()
        raise
