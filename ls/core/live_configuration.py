"""Shared signed configuration revisions, published as one complete owner set.

Trusted configuration authority is supplied explicitly. This owner never creates
keys, enrolls a signer, applies settings, or treats filesystem access as authority.
"""
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
from pathlib import Path
import re
from typing import Callable

from .agent.runtime_lock import runtime_use
from .openpgp.contracts import EnvelopePolicy, LocalTrust, normalize_fingerprint
from .openpgp.envelope import EnvelopeError, seal_envelope
from .openpgp.opening import open_envelope
from .openpgp.container_storage import ensure_private_directory, read_private_file, write_private_file
from .openpgp.secrets import SecretReference, SecretResolver

_SCHEMA = 'localsetup.live-configuration'
_MAX_PAYLOAD = 256 * 1024
_MAX_CIPHERTEXT = 512 * 1024
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_NAME = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')


class ConfigurationError(ValueError):
    """Redacted status; an uncertain candidate is an opaque recovery locator."""
    def __init__(self, code, *, candidate=None):
        self.code, self.candidate = code, candidate
        super().__init__(code)


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _encode(value):
    nodes = 0
    scalar_bytes = 0
    def walk(item, depth=0):
        nonlocal nodes, scalar_bytes
        nodes += 1
        if nodes > 10_000 or depth > 16:
            raise ConfigurationError('configuration_limit')
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ConfigurationError('configuration_invalid')
                walk(key, depth+1)
                walk(child, depth+1)
        elif type(item) is list:
            for child in item:
                walk(child, depth+1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ConfigurationError('configuration_invalid')
        else:
            if type(item) is str and len(item) > _MAX_PAYLOAD:
                raise ConfigurationError('configuration_limit')
            scalar_bytes += len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            if scalar_bytes > _MAX_PAYLOAD:
                raise ConfigurationError('configuration_limit')
    try:
        walk(value)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(raw) > _MAX_PAYLOAD:
            raise ConfigurationError('configuration_limit')
        return raw
    except ConfigurationError:
        raise
    except Exception:
        raise ConfigurationError('configuration_invalid') from None


def _decode(raw):
    try:
        if type(raw) is not bytes or len(raw) > _MAX_PAYLOAD:
            raise ValueError
        value = json.loads(raw)
        # Exact canonical bytes also reject duplicate properties and non-finite
        # numbers, without silently rewriting signed input.
        if _encode(value) != raw:
            raise ValueError
        return value
    except Exception:
        raise ConfigurationError('configuration_invalid') from None


@dataclass(frozen=True, slots=True)
class ConfigurationAuthority:
    """Explicit signer pin and recipient set selected by the configuration owner.

    The authority's signer is deliberately a recipient of its configuration.
    Other recipients can verify/read without holding that signing key.
    current_check supplements signature verification with the owning grant or
    trust-store checks; it must return None or raise, never grant new authority.
    """
    signer: str
    recipients: tuple[str, ...]
    home: Path
    reference: SecretReference
    resolver: SecretResolver
    current_check: Callable[[], None]

    def __post_init__(self):
        try:
            signer = normalize_fingerprint(self.signer)
            recipients = tuple(sorted(normalize_fingerprint(p) for p in self.recipients))
            if (type(self.recipients) is not tuple or not 1 <= len(recipients) <= 64
                    or len(set(recipients)) != len(recipients) or signer not in recipients
                    or not isinstance(self.home, Path) or not self.home.is_absolute()
                    or '..' in self.home.parts or not isinstance(self.reference, SecretReference)
                    or not isinstance(self.resolver, SecretResolver) or not callable(self.current_check)):
                raise ValueError
            object.__setattr__(self, 'signer', signer)
            object.__setattr__(self, 'recipients', recipients)
        except Exception:
            raise ConfigurationError('configuration_authority_invalid') from None

    def check(self):
        try:
            if self.current_check() is not None:
                raise ValueError
        except Exception:
            raise ConfigurationError('configuration_unauthorized') from None

    def seal(self, payload):
        self.check()
        try:
            result = seal_envelope(payload, gnupg_home=self.home, signer_fingerprint=self.signer,
                recipient_fingerprints=self.recipients, local_trust=LocalTrust({self.signer}),
                passphrase_reference=self.reference, secret_resolver=self.resolver, disable_compression=True)
        except Exception:
            raise ConfigurationError('configuration_signing_failed') from None
        self.check()
        if len(result) > _MAX_CIPHERTEXT:
            raise ConfigurationError('configuration_limit')
        return result

    def open(self, serialized):
        self.check()
        if type(serialized) is not bytes or len(serialized) > _MAX_CIPHERTEXT:
            raise ConfigurationError('configuration_limit')
        try:
            payload = open_envelope(serialized, gnupg_home=self.home,
                policy=EnvelopePolicy(expected_signers={self.signer}, expected_recipients=set(self.recipients)),
                local_trust=LocalTrust({self.signer}), passphrase_reference=self.reference,
                secret_resolver=self.resolver)
        except Exception:
            raise ConfigurationError('configuration_verification_failed') from None
        self.check()
        return payload


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    digest: str
    payload: bytes

    @property
    def record(self):
        # Return an independent value; callers cannot mutate accepted bytes.
        return _decode(self.payload)

    @property
    def revision(self):
        return self.record['revision']


class ConfigurationStore:
    """Atomic signed owner-set revisions in an explicitly private state root.

    Cooperating writers use one lease and compare the exact expected revision.
    Local filesystem isolation protects HEAD and intent against rollback; this
    is not distributed freshness or protection against a malicious same-UID peer.
    No history or uncertain candidate is automatically deleted.
    """
    def __init__(self, root: Path, store_id: str, authority: ConfigurationAuthority):
        if (not isinstance(root, Path) or not root.is_absolute() or '..' in root.parts
                or type(store_id) is not str or re.fullmatch(r'[0-9a-f]{32}', store_id) is None
                or store_id == '0'*32 or not isinstance(authority, ConfigurationAuthority)):
            raise ConfigurationError('configuration_invalid')
        self.root, self.store_id, self.authority = root, store_id, authority

    def _metadata(self, name):
        try:
            # The anchored file reader requires an existing private parent.
            # Absence is observed without creating a root or following its leaf.
            self.root.lstat()
            return _decode(read_private_file(self.root/name, maximum=1024))
        except FileNotFoundError:
            return None
        except ConfigurationError:
            raise
        except (OSError, ValueError, EnvelopeError):
            raise ConfigurationError('configuration_unavailable') from None

    def _head(self):
        head = self._metadata('HEAD')
        if head is not None and (type(head) is not dict or set(head) != {'revision', 'digest'}
                or type(head['revision']) is not int or head['revision'] < 1 or not _digest(head['digest'])):
            raise ConfigurationError('configuration_invalid')
        return head

    def _candidate(self, digest):
        if not _digest(digest):
            raise ConfigurationError('configuration_invalid')
        try:
            serialized = read_private_file(self.root/'objects'/digest, maximum=_MAX_CIPHERTEXT)
        except (OSError, ValueError, EnvelopeError):
            raise ConfigurationError('configuration_unavailable') from None
        if hashlib.sha256(serialized).hexdigest() != digest:
            raise ConfigurationError('configuration_changed')
        payload = self.authority.open(serialized)
        return self._validated(payload, digest)

    def _validated(self, payload, digest):
        record = _decode(payload)
        fields = {'schema', 'version', 'store_id', 'revision', 'parent', 'cadence_seconds',
                  'values', 'extensions', 'required', 'provenance'}
        if (type(record) is not dict or set(record) != fields or record['schema'] != _SCHEMA
                or type(record['version']) is not int or record['version'] != 1
                or record['store_id'] != self.store_id
                or type(record['revision']) is not int or record['revision'] < 1
                or (record['parent'] is not None and not _digest(record['parent']))
                or (record['revision'] == 1) != (record['parent'] is None)
                or type(record['cadence_seconds']) is not int or not 1 <= record['cadence_seconds'] <= 3600
                or type(record['values']) is not dict or not 1 <= len(record['values']) <= 64
                or any(_NAME.fullmatch(name) is None or type(value) is not dict
                       for name, value in record['values'].items())
                or type(record['extensions']) is not dict):
            raise ConfigurationError('configuration_invalid')
        provenance = record['provenance']
        if provenance is not None and (type(provenance) is not dict
                or set(provenance) != {'actor', 'machine', 'operation'}
                or any(not _digest(value) for value in provenance.values())):
            raise ConfigurationError('configuration_invalid')
        if record['required'] != ['atomic-owner-set-v1']:
            raise ConfigurationError('configuration_unsupported')
        return ConfigurationSnapshot(digest, payload)

    def load(self, *, minimum_revision=0, ancestor_digest=None):
        if (type(minimum_revision) is not int or minimum_revision < 0
                or ancestor_digest is not None and (not _digest(ancestor_digest) or minimum_revision < 1)):
            raise ConfigurationError('configuration_invalid')
        self.authority.check()
        head = self._head()
        if head is None:
            if minimum_revision:
                raise ConfigurationError('configuration_rollback')
            return None
        candidate = self._candidate(head['digest'])
        if candidate.revision != head['revision'] or candidate.revision < minimum_revision:
            raise ConfigurationError('configuration_rollback')
        if ancestor_digest is not None:
            ancestor = candidate
            for _ in range(1024):
                if ancestor.revision == minimum_revision:
                    break
                previous = self._candidate(ancestor.record['parent'])
                if previous.revision != ancestor.revision-1:
                    raise ConfigurationError('configuration_conflict')
                ancestor = previous
            if ancestor.digest != ancestor_digest:
                raise ConfigurationError('configuration_conflict')
        if self._head() != head:
            raise ConfigurationError('configuration_changed')
        self.authority.check()
        return candidate

    def _intent(self):
        value = self._metadata('intent')
        if value is not None and (type(value) is not dict or set(value) != {'candidate', 'parent'}
                or not _digest(value['candidate']) or value['parent'] is not None and not _digest(value['parent'])):
            raise ConfigurationError('configuration_invalid')
        return value

    def _publish(self, candidate, expected):
        current = self.load()
        if current is not None and current.digest == candidate.digest:
            return current
        if (None if current is None else current.digest) != expected:
            raise ConfigurationError('configuration_conflict')
        record = candidate.record
        if record['parent'] != expected or record['revision'] != (1 if current is None else current.revision+1):
            raise ConfigurationError('configuration_conflict')
        self.authority.check()
        try:
            write_private_file(self.root/'HEAD', _encode({'revision': candidate.revision,
                'digest': candidate.digest}), replace=True)
            result = self.load(minimum_revision=candidate.revision)
            if result.digest != candidate.digest:
                raise ConfigurationError('configuration_changed')
            return result
        except Exception:
            raise ConfigurationError('configuration_uncertain', candidate=candidate.digest) from None

    def commit(self, values, *, expected_digest, authorized: bool,
               cadence_seconds=60, extensions=None, provenance=None):
        if authorized is not True or expected_digest is not None and not _digest(expected_digest):
            raise ConfigurationError('configuration_unauthorized')
        self.authority.check()
        ensure_private_directory(self.root, create=True)
        ensure_private_directory(self.root/'objects', create=True)
        try:
            with runtime_use(self.root, exclusive=True, timeout=0):
                current = self.load()
                if (None if current is None else current.digest) != expected_digest:
                    raise ConfigurationError('configuration_conflict')
                pending = self._intent()
                if pending is not None and pending['candidate'] != expected_digest:
                    raise ConfigurationError('configuration_pending', candidate=pending['candidate'])
                if len(tuple(islice((self.root/'objects').iterdir(), 1024))) >= 1024:
                    raise ConfigurationError('configuration_limit')
                record = dict(schema=_SCHEMA, version=1, store_id=self.store_id,
                    revision=1 if current is None else current.revision+1, parent=expected_digest,
                    cadence_seconds=cadence_seconds, values=values, provenance=provenance,
                    extensions=(current.record['extensions'] if extensions is None and current is not None
                                else {} if extensions is None else extensions), required=['atomic-owner-set-v1'])
                payload = _encode(record)
                self._validated(payload, '0'*64)
                serialized = self.authority.seal(payload)
                digest = hashlib.sha256(serialized).hexdigest()
                try:
                    write_private_file(self.root/'objects'/digest, serialized, replace=False)
                    candidate = self._candidate(digest)
                    if candidate.payload != payload:
                        raise ConfigurationError('configuration_changed')
                    write_private_file(self.root/'intent', _encode({'candidate': digest, 'parent': expected_digest}),
                                       replace=True)
                    return self._publish(candidate, expected_digest)
                except ConfigurationError as error:
                    if error.code == 'configuration_uncertain':
                        raise
                    raise ConfigurationError('configuration_uncertain', candidate=digest) from None
                except Exception:
                    raise ConfigurationError('configuration_uncertain', candidate=digest) from None
        except TimeoutError:
            raise ConfigurationError('configuration_busy') from None

    def recover(self, *, authorized: bool, candidate_digest=None):
        """Finish only the exact retained intent, without signing another set."""
        if authorized is not True:
            raise ConfigurationError('configuration_unauthorized')
        self.authority.check()
        ensure_private_directory(self.root, create=False)
        try:
            with runtime_use(self.root, exclusive=True, timeout=0):
                pending = self._intent()
                if candidate_digest is not None:
                    candidate = self._candidate(candidate_digest)
                    current = self.load()
                    current_digest = None if current is None else current.digest
                    if pending is not None and pending['candidate'] not in (current_digest, candidate_digest):
                        raise ConfigurationError('configuration_conflict')
                    if current_digest not in (candidate.digest, candidate.record['parent']):
                        raise ConfigurationError('configuration_conflict')
                    pending = {'candidate': candidate.digest, 'parent': candidate.record['parent']}
                    self.authority.check()
                    write_private_file(self.root/'intent', _encode(pending), replace=True)
                if pending is None:
                    raise ConfigurationError('configuration_pending')
                return self._publish(self._candidate(pending['candidate']), pending['parent'])
        except TimeoutError:
            raise ConfigurationError('configuration_busy') from None
