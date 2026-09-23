"""Anchored regular-file operations under explicit task authority and leases."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
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
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..openpgp.contracts import EnvelopeHeader, EnvelopePolicy, LocalTrust
from ..openpgp.envelope import MAX_PAYLOAD_BYTES
from ..openpgp.opening import open_envelope
from ..openpgp.secrets import SecretReference, SecretResolver
from .file_grants import FileGrant
from .runtime_lock import runtime_use

MAX_FILE = 8 * 1024 * 1024
MAX_PAGE_BYTES = 8 * 1024
MAX_PAGE_LINES = 200
MAX_PAGE_RESPONSE = 16 * 1024
_VERIFIED_CACHE_TTL_SECONDS = 30.0
_OPEN_ATTEMPT_WINDOW_SECONDS = 60.0
_MAX_OPEN_ATTEMPTS_PER_WINDOW = 2
_MAX_PROTECTED_PAGES = 2048


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _grant_fingerprint(grant):
    value = {
        'task': grant.task, 'session': grant.session, 'root': str(grant.root),
        'read': grant.read, 'write': grant.write, 'disclose': grant.disclose,
        'expires': grant.expires,
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _page_authority(grant, task, session, name, provider, openpgp_context=None):
    value = {
        'task': task, 'session': session, 'path': name,
        'grant': _grant_fingerprint(grant), 'provider': provider,
    }
    if openpgp_context is not None:
        value['openpgp'] = openpgp_context
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _openpgp_context(gnupg_home, policy, local_trust, passphrase_reference, secret_resolver):
    supplied = (gnupg_home, policy, local_trust, passphrase_reference, secret_resolver)
    if not any(value is not None for value in supplied):
        return None
    if (
        gnupg_home is None
        or not isinstance(policy, EnvelopePolicy)
        or not isinstance(local_trust, LocalTrust)
        or (passphrase_reference is None) != (secret_resolver is None)
        or (passphrase_reference is not None and not isinstance(passphrase_reference, SecretReference))
        or (secret_resolver is not None and not isinstance(secret_resolver, SecretResolver))
    ):
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust')
    try:
        home = os.fspath(gnupg_home)
    except Exception:
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust') from None
    if not isinstance(home, str) or not home:
        raise ValueError('OpenPGP read authority requires a selected home, policy, and local trust')
    value = {
        'home': home,
        'signers': sorted(policy.expected_signers),
        'recipients': sorted(policy.expected_recipients),
        'owner': policy.owner_fingerprint,
        'publisher': policy.publisher_fingerprint,
        'trust': sorted(local_trust.fingerprints),
        'passphrase': None if passphrase_reference is None else {
            'provider': passphrase_reference.provider.value,
            'name': passphrase_reference.name,
        },
        'resolver': None if secret_resolver is None else (
            type(secret_resolver).__module__, type(secret_resolver).__qualname__,
        ),
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


_OPENPGP_HEADER = EnvelopeHeader().to_json().encode('utf-8')
_OPENPGP_FORMAT_MARKER = b'localsetup.openpgp'


def _is_openpgp_candidate(prefix):
    prefix = prefix[:258]
    if len(prefix) < 2:
        return False
    if not any(_binary_control_byte(value) for value in prefix):
        try:
            codecs.getincrementaldecoder('utf-8')('strict').decode(prefix, final=False)
        except UnicodeDecodeError:
            pass
        else:
            return False
    advertised_length = int.from_bytes(prefix[:2], 'big')
    available_header = prefix[2:]
    if advertised_length == len(_OPENPGP_HEADER):
        return True
    if 0 < advertised_length <= 256:
        header = available_header[:advertised_length]
        if len(header) == advertised_length:
            try:
                parsed = json.loads(header.decode('utf-8'))
            except (UnicodeDecodeError, ValueError, RecursionError):
                parsed = None
            if type(parsed) is dict:
                return True
        if header.lstrip(b' \t\r\n').startswith(b'{') or (
            header and (
                header.startswith(_OPENPGP_HEADER)
                or _OPENPGP_HEADER.startswith(header)
            )
        ):
            return True
    return _OPENPGP_FORMAT_MARKER in available_header


@dataclass(slots=True, repr=False)
class _VerifiedPageCache:
    authority: str
    source_stat: tuple[int, ...]
    source_digest: str
    plaintext_digest: str
    revision: str
    plaintext: bytearray
    text: bool
    expires_at: float
    next_cursor: str | None
    stream_nonce: str
    pages_served: int


def _open_verified_envelope(serialized, *, gnupg_home, policy, local_trust,
                            passphrase_reference, secret_resolver):
    failed = False
    plaintext = None
    try:
        plaintext = open_envelope(
            serialized,
            gnupg_home=gnupg_home,
            policy=policy,
            local_trust=local_trust,
            passphrase_reference=passphrase_reference,
            secret_resolver=secret_resolver,
        )
    except Exception as error:
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        failed = True
    if failed or type(plaintext) is not bytes:
        serialized = b''
        plaintext = None
        gnupg_home = policy = local_trust = passphrase_reference = secret_resolver = None
        raise PermissionError('Protected OpenPGP envelope could not be verified')
    return plaintext


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


def _text_content(data):
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    valid_utf8, binary = True, False
    for start in range(0, len(data), 65536):
        chunk = data[start:start + 65536]
        if valid_utf8:
            try:
                decoder.decode(chunk, final=False)
            except UnicodeDecodeError:
                valid_utf8 = False
        if not binary and any(_binary_control_byte(value) for value in chunk):
            binary = True
    if valid_utf8:
        try:
            decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            valid_utf8 = False
    return valid_utf8 and not binary


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
        self._page_state_lock = threading.RLock()
        self._verified_page_cache: _VerifiedPageCache | None = None
        self._open_attempts: deque[float] = deque()

    def _wipe_verified_page_cache(self) -> None:
        cache = self._verified_page_cache
        if cache is None:
            return
        payload = cache.plaintext
        for start in range(0, len(payload), 65536):
            end = min(start + 65536, len(payload))
            payload[start:end] = bytes(end - start)
        payload.clear()
        self._verified_page_cache = None

    def _expire_verified_page_cache(self, now: float) -> None:
        cache = self._verified_page_cache
        if cache is not None and now >= cache.expires_at:
            self._wipe_verified_page_cache()

    def _record_fresh_open(self, now: float) -> None:
        while self._open_attempts and now - self._open_attempts[0] >= _OPEN_ATTEMPT_WINDOW_SECONDS:
            self._open_attempts.popleft()
        if len(self._open_attempts) >= _MAX_OPEN_ATTEMPTS_PER_WINDOW:
            raise PermissionError('OpenPGP decryption budget exceeded')
        self._open_attempts.append(now)

    def clear_page_cache(self) -> None:
        """Wipe retained verified plaintext when the owning lease is replaced or ends."""
        with self._page_state_lock:
            self._wipe_verified_page_cache()

    def _encode_page_cursor(self, authority, revision, offset, stream_nonce=None):
        claims = {'v': 1, 'a': authority, 'r': revision, 'o': offset}
        if stream_nonce is not None:
            claims['n'] = stream_nonce
        payload = _json_bytes(claims)
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
            fields = set(claims)
            if fields not in ({'v', 'a', 'r', 'o'}, {'v', 'a', 'r', 'o', 'n'}):
                raise ValueError
            if (type(claims['v']) is not int or claims['v'] != 1
                    or claims['a'] != authority or type(claims['o']) is not int
                    or claims['o'] < 0 or not isinstance(claims['r'], str)
                    or len(claims['r']) != 64
                    or any(c not in '0123456789abcdef' for c in claims['r'])):
                raise ValueError
            stream_nonce = claims.get('n')
            if stream_nonce is not None and (
                type(stream_nonce) is not str or len(stream_nonce) != 22
                or any(char not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for char in stream_nonce)
            ):
                raise ValueError
            return claims['r'], claims['o'], stream_nonce

        except (ValueError, TypeError, KeyError, json.JSONDecodeError, RecursionError, binascii.Error):
            raise PermissionError(message) from None

    def _page_response(self, data, *, text, offset, size, authority, revision, stream_nonce=None):

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
        next_cursor = (
            self._encode_page_cursor(authority, revision, next_offset, stream_nonce)
            if next_offset < size else None
        )
        result = {
            'content': content, 'encoding': encoding, 'bytes': len(data),
            'revision': revision, 'next_cursor': next_cursor,
        }
        return result

    def read_page(self, task: str, session: str, name: str, cursor: str | None = None,
                  *, for_provider: bool = False, gnupg_home=None,
                  policy: EnvelopePolicy | None = None, local_trust: LocalTrust | None = None,
                  passphrase_reference: SecretReference | None = None,
                  secret_resolver: SecretResolver | None = None) -> dict[str, str | int | None]:
        """Return one bounded page; protected cursors share a short-lived verified cache."""
        with self._page_state_lock:
            self._expire_verified_page_cache(time.monotonic())
            try:
                return self._read_page_locked(
                    task, session, name, cursor,
                    for_provider=for_provider,
                    gnupg_home=gnupg_home,
                    policy=policy,
                    local_trust=local_trust,
                    passphrase_reference=passphrase_reference,
                    secret_resolver=secret_resolver,
                )
            except Exception:
                self._wipe_verified_page_cache()
                raise
            finally:
                gnupg_home = policy = local_trust = passphrase_reference = secret_resolver = None

    def _read_page_locked(self, task: str, session: str, name: str, cursor: str | None = None,
                          *, for_provider: bool = False, gnupg_home=None,
                          policy: EnvelopePolicy | None = None, local_trust: LocalTrust | None = None,
                          passphrase_reference: SecretReference | None = None,
                          secret_resolver: SecretResolver | None = None) -> dict[str, str | int | None]:
        """Return one bounded, lossless page under its live grant and lease.

        Envelope candidates are passed only to L08's complete verifier.
        Continuations reread and hash the anchored source, then reuse only the
        matching per-broker verified payload while reauthorizing the grant.
        """
        context_failed = False
        try:
            openpgp_context = _openpgp_context(
                gnupg_home, policy, local_trust, passphrase_reference, secret_resolver,
            )
        except Exception as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            context_failed = True
        if context_failed:
            gnupg_home = policy = local_trust = passphrase_reference = secret_resolver = None
            raise ValueError('Invalid OpenPGP read authority')
        with self._target(task, session, 'read', name, for_provider) as (directory, leaf):
            authority = _page_authority(
                self.grant, task, session, name, for_provider, openpgp_context,
            )
            expected_revision, offset, expected_nonce = (
                (None, 0, None) if cursor is None else self._decode_page_cursor(cursor, authority)
            )
            if cursor is None:
                self._wipe_verified_page_cache()
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                raw_page = bytearray()
                encrypted_source = None
                plaintext = None
                page = response = candidate = best = None
                first_chunk = chunk = serialized = b''
                before = os.fstat(fd)
                _regular(before)
                if before.st_size > MAX_FILE:
                    raise ValueError('Broker file exceeds 8 MiB')
                first_chunk = os.read(fd, 65536)
                encrypted = _is_openpgp_candidate(first_chunk)
                if encrypted and openpgp_context is None:
                    raise PermissionError('Protected OpenPGP envelope requires explicit decryption authority')

                content_hash = hashlib.sha256()
                decoder = codecs.getincrementaldecoder('utf-8')('strict')
                valid_utf8, binary = True, False
                encrypted_source = bytearray() if encrypted else None
                position = 0
                chunk = first_chunk
                while chunk:
                    content_hash.update(chunk)
                    chunk_end = position + len(chunk)
                    if chunk_end > MAX_FILE:
                        raise ValueError('Broker file exceeds 8 MiB')
                    if encrypted:
                        encrypted_source.extend(chunk)
                    else:
                        if valid_utf8:
                            try:
                                decoder.decode(chunk, final=False)
                            except UnicodeDecodeError:
                                valid_utf8 = False
                        if not binary and any(_binary_control_byte(value) for value in chunk):
                            binary = True
                        start, end = max(offset, position), min(offset + MAX_PAGE_BYTES, chunk_end)
                        if start < end:
                            raw_page.extend(chunk[start - position:end - position])
                    position = chunk_end
                    chunk = os.read(fd, 65536)

                if not encrypted and valid_utf8:
                    try:
                        decoder.decode(b'', final=True)
                    except UnicodeDecodeError:
                        valid_utf8 = False

                after = os.fstat(fd)
                if _revision_stat(before) != _revision_stat(after) or position != after.st_size:
                    raise PermissionError('Broker file changed while paging')
                source_digest = content_hash.hexdigest()
                size = position
                revision_value = {
                    'stat': _revision_stat(before), 'content': source_digest,
                }

                stream_nonce = None
                pages_served = 0
                if encrypted:
                    self.grant.check(task, session, 'read', name, provider=for_provider)
                    source_stat = _revision_stat(before)
                    cache = self._verified_page_cache
                    if cursor is not None:
                        if (
                            expected_nonce is None or cache is None
                            or cache.authority != authority
                            or cache.source_stat != source_stat
                            or cache.source_digest != source_digest
                            or cache.revision != expected_revision
                            or cache.next_cursor != cursor
                            or cache.stream_nonce != expected_nonce
                        ):
                            raise PermissionError('Page cursor is stale or unauthorized')
                        plaintext = cache.plaintext
                        size = len(plaintext)
                        text = cache.text
                        stream_nonce = cache.stream_nonce
                        pages_served = cache.pages_served
                    else:
                        self._record_fresh_open(time.monotonic())
                        serialized = bytes(encrypted_source)
                        encrypted_source.clear()
                        encrypted_source = None
                        try:
                            plaintext = _open_verified_envelope(
                                serialized,
                                gnupg_home=gnupg_home,
                                policy=policy,
                                local_trust=local_trust,
                                passphrase_reference=passphrase_reference,
                                secret_resolver=secret_resolver,
                            )
                        finally:
                            serialized = b''
                        if len(plaintext) > MAX_PAYLOAD_BYTES:
                            raise ValueError('Verified OpenPGP payload exceeds the 16 MiB limit')
                        size = len(plaintext)
                        stream_nonce = secrets.token_urlsafe(16)
                        revision_value.update({
                            'verified_plaintext': hashlib.sha256(plaintext).hexdigest(),
                            'openpgp_context': openpgp_context,
                        })
                        for start in range(0, size, 65536):
                            chunk = plaintext[start:start + 65536]
                            if valid_utf8:
                                try:
                                    decoder.decode(chunk, final=False)
                                except UnicodeDecodeError:
                                    valid_utf8 = False
                            if not binary and any(_binary_control_byte(value) for value in chunk):
                                binary = True
                        if valid_utf8:
                            try:
                                decoder.decode(b'', final=True)
                            except UnicodeDecodeError:
                                valid_utf8 = False
                    if cursor is not None:
                        revision_value.update({
                            'verified_plaintext': cache.plaintext_digest,
                            'openpgp_context': openpgp_context,
                        })
                    self.grant.check(task, session, 'read', name, provider=for_provider)
                    latest = os.fstat(fd)
                    if _revision_stat(after) != _revision_stat(latest) or position != latest.st_size:
                        raise PermissionError('Broker file changed while paging')
                    if offset < size:
                        raw_page.extend(plaintext[offset:min(offset + MAX_PAGE_BYTES, size)])

                revision = hashlib.sha256(_json_bytes(revision_value)).hexdigest()
                if expected_revision is not None and expected_revision != revision:
                    raise PermissionError('Page cursor is stale or unauthorized')
                if offset > size:
                    raise PermissionError('Invalid or unauthorized page cursor')
                self.grant.check(task, session, 'read', name, provider=for_provider)

                text = cache.text if encrypted and cursor is not None else valid_utf8 and not binary
                page = bytes(raw_page)
                response = self._page_response(
                    page, text=text, offset=offset, size=size,
                    authority=authority, revision=revision, stream_nonce=stream_nonce,
                )
                if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                    low, high = 0, len(page)
                    best = self._page_response(
                        b'', text=text, offset=offset, size=size,
                        authority=authority, revision=revision, stream_nonce=stream_nonce,
                    )
                    while low < high:
                        middle = (low + high + 1) // 2
                        candidate = self._page_response(
                            page[:middle], text=text, offset=offset, size=size,
                            authority=authority, revision=revision, stream_nonce=stream_nonce,
                        )
                        if len(_json_bytes(candidate)) <= MAX_PAGE_RESPONSE:
                            low, best = middle, candidate
                        else:
                            high = middle - 1
                    response = self._page_response(
                        page[:low], text=text, offset=offset, size=size,
                        authority=authority, revision=revision, stream_nonce=stream_nonce,
                    )
                    if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                        response = best
                self.grant.check(task, session, 'read', name, provider=for_provider)
                if len(_json_bytes(response)) > MAX_PAGE_RESPONSE:
                    raise ValueError('Broker page response exceeds 16 KiB')
                if response['bytes'] == 0 and response['next_cursor'] is not None:
                    raise ValueError('Broker page cannot return an empty continuation')
                if encrypted and response['next_cursor'] is not None:
                    if pages_served + 1 >= _MAX_PROTECTED_PAGES:
                        raise PermissionError(f'Protected page chain exceeds the {_MAX_PROTECTED_PAGES}-page limit')
                    if cursor is None:
                        cache_plaintext = bytearray(plaintext)
                        self._verified_page_cache = _VerifiedPageCache(
                            authority=authority,
                            source_stat=source_stat,
                            source_digest=source_digest,
                            plaintext_digest=revision_value['verified_plaintext'],
                            revision=revision,
                            plaintext=cache_plaintext,
                            text=text,
                            expires_at=time.monotonic() + _VERIFIED_CACHE_TTL_SECONDS,
                            next_cursor=response['next_cursor'],
                            stream_nonce=stream_nonce,
                            pages_served=1,
                        )
                        plaintext = None
                    else:
                        cache.pages_served += 1
                        cache.next_cursor = response['next_cursor']
                elif encrypted and cursor is not None:
                    self._wipe_verified_page_cache()
                return response
            finally:
                if encrypted_source is not None:
                    encrypted_source.clear()
                raw_page.clear()
                plaintext = page = response = candidate = best = None
                first_chunk = chunk = serialized = b''
                gnupg_home = policy = local_trust = passphrase_reference = secret_resolver = None
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
