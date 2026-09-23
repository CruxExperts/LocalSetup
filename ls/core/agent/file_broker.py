"""Anchored regular-file operations under explicit task authority and leases."""
from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .file_grants import FileGrant
from .runtime_lock import runtime_use

MAX_FILE = 8 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024
MAX_PAGE_LINES = 200
MAX_PAGE_RESPONSE = 16 * 1024


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _grant_fingerprint(grant):
    value = {
        'task': grant.task, 'session': grant.session, 'root': str(grant.root),
        'read': grant.read, 'write': grant.write, 'disclose': grant.disclose,
        'expires': grant.expires,
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _page_authority(grant, task, session, name, provider):
    value = {
        'task': task, 'session': session, 'path': name,
        'grant': _grant_fingerprint(grant), 'provider': provider,
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _revision_stat(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def _limit_page_lines(data):
    end = 0
    for _ in range(MAX_PAGE_LINES):
        newline = data.find(b'\n', end)
        if newline < 0:
            return data
        end = newline + 1
    return data[:end]


def _binary_control_byte(value):
    return value == 0 or value == 127 or (value < 32 and value not in (9, 10, 12, 13))


def _b64url_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64url_decode(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


@contextmanager
def parent(root: Path, parts: tuple[str, ...]):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *parts[:-1]):
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o7000:
        raise PermissionError('Broker requires an owned regular single-link file without special modes')


class FileBroker:
    def __init__(self, grant: FileGrant, lease_root: Path):
        if lease_root.resolve().is_relative_to(grant.root.resolve()) or grant.root.resolve().is_relative_to(lease_root.resolve()):
            raise ValueError('Broker lease state and granted tree must be separate')
        self.grant, self.lease_root = grant, lease_root
        self._cursor_key = secrets.token_bytes(32)

    def _encode_page_cursor(self, authority, revision, offset):
        payload = _json_bytes({'v': 1, 'a': authority, 'r': revision, 'o': offset})
        signature = hmac.digest(self._cursor_key, payload, 'sha256')
        return f'{_b64url_encode(payload)}.{_b64url_encode(signature)}'

    def _decode_page_cursor(self, cursor, authority):
        message = 'Invalid or unauthorized page cursor'
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
            raise PermissionError(message)
        try:
            encoded, signed = cursor.split('.', 1)
            payload, signature = _b64url_decode(encoded), _b64url_decode(signed)
            expected = hmac.digest(self._cursor_key, payload, 'sha256')
            if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
                raise ValueError
            claims = json.loads(payload)
            if (set(claims) != {'v', 'a', 'r', 'o'} or type(claims['v']) is not int or claims['v'] != 1
                    or claims['a'] != authority or type(claims['o']) is not int
                    or claims['o'] < 0 or not isinstance(claims['r'], str)
                    or len(claims['r']) != 64
                    or any(c not in '0123456789abcdef' for c in claims['r'])):
                raise ValueError
            return claims['r'], claims['o']
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, RecursionError, binascii.Error):
            raise PermissionError(message) from None

    def _page_response(self, data, *, text, offset, size, authority, revision):
        if text:
            while data:
                try:
                    content = data.decode('utf-8')
                    break
                except UnicodeDecodeError as exc:
                    if exc.end != len(data):
                        raise PermissionError('File changed while paging')
                    data = data[:exc.start]
            else:
                content = ''
            data = _limit_page_lines(data)
            content = data.decode('utf-8')
            encoding = 'utf-8'
        else:
            data = _limit_page_lines(data)
            content = base64.b64encode(data).decode('ascii')
            encoding = 'base64'
        next_offset = offset + len(data)
        next_cursor = self._encode_page_cursor(authority, revision, next_offset) if next_offset < size else None
        result = {
            'content': content, 'encoding': encoding, 'bytes': len(data),
            'revision': revision, 'next_cursor': next_cursor,
        }
        return result

    def read_page(self, task: str, session: str, name: str, cursor: str | None = None,
                  *, for_provider: bool = False) -> dict[str, str | int | None]:
        """Return one bounded, lossless page and an authenticated continuation cursor.

        The result contains ``content``, ``encoding`` (``utf-8`` or ``base64``),
        raw ``bytes`` in this page, a content-and-file ``revision`` digest, and
        ``next_cursor`` (or ``None``). Each continuation rechecks the live grant
        and streams the anchored file to reject any revision change.
        """
        with self._target(task, session, 'read', name, for_provider) as (directory, leaf):
            authority = _page_authority(self.grant, task, session, name, for_provider)
            expected_revision, offset = (None, 0) if cursor is None else self._decode_page_cursor(cursor, authority)
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                _regular(before)
                if before.st_size > MAX_FILE:
                    raise ValueError('Broker file exceeds 8 MiB')
                content_hash = hashlib.sha256()
                decoder = codecs.getincrementaldecoder('utf-8')('strict')
                valid_utf8, binary = True, False
                raw_page = bytearray()
                position = 0
                while chunk := os.read(fd, 65536):
                    content_hash.update(chunk)
                    if valid_utf8:
                        try:
                            decoder.decode(chunk, final=False)
                        except UnicodeDecodeError:
                            valid_utf8 = False
                    if not binary and any(_binary_control_byte(value) for value in chunk):
                        binary = True
                    chunk_end = position + len(chunk)
                    start, end = max(offset, position), min(offset + MAX_PAGE_BYTES, chunk_end)
                    if start < end:
                        raw_page.extend(chunk[start - position:end - position])
                    position = chunk_end
                    if position > MAX_FILE:
                        raise ValueError('Broker file exceeds 8 MiB')
                if valid_utf8:
                    try:
                        decoder.decode(b'', final=True)
                    except UnicodeDecodeError:
                        valid_utf8 = False
                after = os.fstat(fd)
                if _revision_stat(before) != _revision_stat(after) or position != after.st_size:
                    raise PermissionError('Broker file changed while paging')
                revision = hashlib.sha256(_json_bytes({
                    'stat': _revision_stat(before), 'content': content_hash.hexdigest(),
                })).hexdigest()
                if expected_revision is not None and expected_revision != revision:
                    raise PermissionError('Page cursor is stale or unauthorized')
                if offset > after.st_size:
                    raise PermissionError('Invalid or unauthorized page cursor')
                self.grant.check(task, session, 'read', name, provider=for_provider)

                text = valid_utf8 and not binary
                page = bytes(raw_page)
                response = self._page_response(
                    page, text=text, offset=offset, size=after.st_size,
                    authority=authority, revision=revision,
                )
                if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                    low, high = 0, len(page)
                    best = self._page_response(
                        b'', text=text, offset=offset, size=after.st_size,
                        authority=authority, revision=revision,
                    )
                    while low < high:
                        middle = (low + high + 1) // 2
                        candidate = self._page_response(
                            page[:middle], text=text, offset=offset, size=after.st_size,
                            authority=authority, revision=revision,
                        )
                        if len(_json_bytes(candidate)) <= MAX_PAGE_RESPONSE:
                            low, best = middle, candidate
                        else:
                            high = middle - 1
                    response = self._page_response(
                        page[:low], text=text, offset=offset, size=after.st_size,
                        authority=authority, revision=revision,
                    )
                    if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                        response = best
                self.grant.check(task, session, 'read', name, provider=for_provider)
                if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                    raise ValueError('Broker page response exceeds 16 KiB')
                if response['bytes'] == 0 and response['next_cursor'] is not None:
                    raise ValueError('Broker page cannot return an empty continuation')
                return response
            finally:
                os.close(fd)

    @contextmanager
    def _target(self, task, session, operation, name, provider=False):
        parts = self.grant.check(task, session, operation, name, provider=provider)
        with runtime_use(self.lease_root, exclusive=operation == 'write', timeout=max(0, self.grant.expires-time.monotonic())):
            self.grant.check(task, session, operation, name, provider=provider)
            with parent(self.grant.root, parts) as target:
                yield target

    def read(self, task: str, session: str, name: str, *, for_provider: bool = False) -> bytes:
        return self.read_entry(task, session, name, for_provider=for_provider)[0]

    def read_entry(self, task: str, session: str, name: str, *, for_provider: bool = False) -> tuple[bytes, int]:
        """Read coherent bytes and source mode under the same anchored descriptor."""
        with self._target(task, session, 'read', name, for_provider) as (directory, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                before = os.fstat(fd)
                _regular(before)
                data = bytearray()
                while chunk := os.read(fd, min(65536, MAX_FILE + 1 - len(data))):
                    data.extend(chunk)
                    if len(data) > MAX_FILE:
                        raise ValueError('Broker file exceeds 8 MiB')
                after = os.fstat(fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise PermissionError('Broker file changed during read')
                self.grant.check(task, session, 'read', name, provider=for_provider)
                return bytes(data), stat.S_IMODE(before.st_mode)
            finally:
                os.close(fd)


    def write(self, task: str, session: str, name: str, data: bytes) -> None:
        self._write(task, session, name, data)

    def write_recorded(self, task: str, session: str, name: str, data: bytes, *, expected_before: str | None, journal, checkpoint: str | None = None, tool_call: dict | None = None) -> str:
        from .file_recovery import journal_binding
        journal_binding(self, journal, task, session)
        self.grant.check(task, session, 'read', name)
        if expected_before is not None and (not isinstance(expected_before, str) or len(expected_before) != 64 or any(c not in '0123456789abcdef' for c in expected_before)):
            raise ValueError('Expected file precondition must be SHA-256 or absence')
        return self._write(task, session, name, data, journal=journal, expected_before=expected_before, checkpoint=checkpoint, tool_call=tool_call)

    def _write(self, task, session, name, data, *, journal=None, expected_before=None, checkpoint=None, tool_call=None):
        if not isinstance(data, bytes) or len(data) > MAX_FILE:
            raise ValueError('Broker replacement must be bytes within 8 MiB')
        recorded_root = None
        if journal is not None:
            from .file_recovery import root_digest
            recorded_root = root_digest(self.grant.root)
        with self._target(task, session, 'write', name) as (directory, leaf):
            original, attributes, before_digest, operation = None, {}, None, None
            before_properties = None
            try:
                source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                pass
            else:
                try:
                    original = os.fstat(source)
                    _regular(original)
                    if journal is not None:
                        before_digest = digest_descriptor(source, original)
                    if hasattr(os, 'listxattr'):
                        attributes = {key: os.getxattr(source, key) for key in os.listxattr(source)}
                    if journal is not None:
                        before_properties = properties_digest(source, original, attributes)
                finally:
                    os.close(source)
            if journal is not None and before_digest != expected_before:
                raise PermissionError('File content differs from the expected precondition')
            temporary = '.lscli-write-' + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    if original is not None:
                        os.fchmod(stream.fileno(), stat.S_IMODE(original.st_mode))
                        os.fchown(stream.fileno(), original.st_uid, original.st_gid)
                    if original is not None and hasattr(os, 'listxattr'):
                        for key in os.listxattr(stream.fileno()):
                            if key not in attributes:
                                os.removexattr(stream.fileno(), key)
                    for key, value in attributes.items():
                        os.setxattr(stream.fileno(), key, value)
                    os.fsync(stream.fileno())
                    after_properties = properties_digest(stream.fileno()) if journal is not None else None
                def check_target():
                    self.grant.check(task, session, 'write', name)
                    if journal is not None and root_digest(self.grant.root) != recorded_root:
                        raise PermissionError('Workspace identity changed during replacement')
                    with parent(self.grant.root, tuple(Path(name).parts)) as (current_directory, _):
                        old_parent, new_parent = os.fstat(directory), os.fstat(current_directory)
                        if (old_parent.st_dev, old_parent.st_ino) != (new_parent.st_dev, new_parent.st_ino):
                            raise PermissionError('Broker parent changed during replacement')
                    try:
                        current = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if (current is None) != (original is None) or (current is not None and (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (original.st_dev, original.st_ino, original.st_mtime_ns, original.st_ctime_ns)):
                        raise PermissionError('Broker target changed during replacement')
                check_target()
                if journal is not None:
                    self.grant.check(task, session, 'read', name)
                    operation = journal.begin('file_replace', {'path': name, 'before': before_digest,
                        'after': hashlib.sha256(data).hexdigest(), 'root_sha256': recorded_root,
                        'before_properties': before_properties, 'after_properties': after_properties},
                        checkpoint=checkpoint, tool_call=tool_call, timeout=max(0, self.grant.expires-time.monotonic()))
                    check_target()
                os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
                if journal is not None:
                    journal.finish(operation, 'applied', evidence_sha256=hashlib.sha256(data).hexdigest(),
                                   timeout=max(0, self.grant.expires-time.monotonic()))
                return operation
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass


def digest_descriptor(fd, before) -> str:
    """Hash a bounded regular descriptor and reject observed content changes."""
    digest, size = hashlib.sha256(), 0
    while chunk := os.read(fd, 65536):
        size += len(chunk)
        if size > MAX_FILE:
            raise ValueError('Recorded file precondition exceeds 8 MiB')
        digest.update(chunk)
    after = os.fstat(fd)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise PermissionError('File changed while checking its precondition')
    return digest.hexdigest()


def properties_digest(fd, info=None, attributes=None) -> str:
    info = info or os.fstat(fd)
    if attributes is None:
        attributes = {key: os.getxattr(fd, key) for key in os.listxattr(fd)} if hasattr(os, 'listxattr') else {}
    value = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
             'xattrs': {key: hashlib.sha256(data).hexdigest() for key, data in attributes.items()}}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
