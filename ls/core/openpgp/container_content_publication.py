"""Exact ciphertext delivery without relying on conditional carrier writes."""
from __future__ import annotations

import os
from pathlib import Path
import time
from typing import TYPE_CHECKING, Callable

from .envelope_io import AnchoredFile

if TYPE_CHECKING:
    from .container_transfer import ContentAddressedCipherPublisher, PublicationResult


def publish_content_addressed(
    source_path: Path, *, destination: ContentAddressedCipherPublisher,
    key: str, length: int, sha256: str, refresh: Callable[[], None],
    check: Callable[[], None], deadline: float, reconcile_only: bool = False,
) -> PublicationResult:
    # A distinct ciphertext gets a distinct location, even when concurrent
    # writers use the same logical key. No mutable head is written here.
    from .container_transfer import PublicationResult, TransferError, _CheckedReader, _hash_anchored
    if (not callable(getattr(destination, 'observe', None))
            or not reconcile_only and not callable(getattr(destination, 'put', None))):
        raise TransferError('publication_invalid')
    locator = key + '.sha256-' + sha256

    def cancelled():
        try:
            check()
            return False
        except TransferError:
            return True

    def observe():
        check()
        try:
            result = destination.observe(locator, timeout_seconds=deadline-time.monotonic(),
                                         cancel_check=cancelled)
        except Exception:
            raise TransferError('publish_uncertain') from None
        check()
        if result is not None and result != (length, sha256):
            raise TransferError('ciphertext_collision')
        return result

    if observe() is not None:
        refresh()
        check()
        return PublicationResult('identical', key, sha256, locator)
    if reconcile_only:
        raise TransferError('publish_uncertain')
    dispatched, acknowledged = False, False
    try:
        with AnchoredFile(source_path, private=True) as source:
            if _hash_anchored(source, length, check) != (length, sha256):
                raise TransferError('ciphertext_invalid')
            os.lseek(source.file, 0, os.SEEK_SET)
            stream = _CheckedReader(source, check)
            check()
            dispatched = True
            destination.put(locator, stream, length=length, sha256=sha256,
                            timeout_seconds=deadline-time.monotonic(), cancel_check=cancelled)
            check()
            acknowledged = (stream.count, stream.digest.hexdigest()) == (length, sha256)
            if not source.unchanged():
                raise TransferError('ciphertext_invalid')
    except Exception:
        if not dispatched:
            raise
        acknowledged = False
    # An acknowledgement alone cannot establish what a nonconditional carrier
    # retained. An absent/unknown readback never causes a second put.
    try:
        if observe() is None:
            raise TransferError('publish_uncertain')
        refresh()
        check()
    except TransferError as exc:
        if exc.code == 'ciphertext_collision':
            raise
        raise TransferError('publish_uncertain') from None
    except Exception:
        raise TransferError('publish_uncertain') from None
    return PublicationResult('verified' if acknowledged else 'observed_after_uncertain', key, sha256, locator)
