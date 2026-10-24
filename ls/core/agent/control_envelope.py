"""Explicitly admitted OpenPGP peers and bounded remote-control bindings."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time

from .broker_rpc import _decode
from .operation_journal import IDENTIFIER
from ..openpgp import envelope, opening, trust_state
from ..openpgp.contracts import EnvelopePolicy, LocalTrust, normalize_fingerprint
from ..openpgp.secrets import SecretReference, SecretResolver

MAX_PAYLOAD = 256*1024
MAX_CIPHERTEXT = 1024*1024


def encode(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'),
                     ensure_ascii=True, allow_nan=False).encode('ascii')
    if len(raw) > MAX_PAYLOAD:
        raise ValueError('Control payload exceeds limit')
    return raw


@dataclass(frozen=True)
class ControlBinding:
    agent: str
    machine: str
    scope: tuple[str, ...]
    task: str
    session: str
    run: str
    operation: str
    owner_generation: str
    issued_at: int
    expires_at: int

    def __post_init__(self):
        if (type(self.scope) is not tuple or not 1 <= len(self.scope) <= 16
                or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
                       for value in (*self.scope, self.agent, self.machine, self.task,
                                     self.session, self.run, self.operation, self.owner_generation))
                or type(self.issued_at) is not int or type(self.expires_at) is not int
                or not 0 <= self.issued_at < self.expires_at
                or self.expires_at-self.issued_at > 86400):
            raise ValueError('Invalid remote control binding')

    def value(self):
        return json.loads(encode(asdict(self)))

    def digest(self):
        return hashlib.sha256(encode(self.value())).hexdigest()

    def check(self):
        if not self.issued_at <= time.time() < self.expires_at:
            raise PermissionError('Remote control binding is not current')


@dataclass(frozen=True)
class RemotePeer:
    gnupg_home: Path
    trust_state_path: Path
    store_id: str
    revision: int
    epoch: int
    scope: tuple[str, ...]
    local_role: str
    local_fingerprint: str
    remote_role: str
    remote_fingerprint: str
    passphrase_reference: SecretReference
    secret_resolver: SecretResolver

    def __post_init__(self):
        if ({self.local_role, self.remote_role} != {'owner', 'publisher'}
                or type(self.revision) is not int or self.revision < 0
                or type(self.epoch) is not int or self.epoch < 0
                or type(self.scope) is not tuple or not self.scope
                or not isinstance(self.passphrase_reference, SecretReference)
                or not isinstance(self.secret_resolver, SecretResolver)):
            raise ValueError('Invalid remote peer admission')
        for name in ('local_fingerprint', 'remote_fingerprint'):
            object.__setattr__(self, name, normalize_fingerprint(getattr(self, name)))
        if self.local_fingerprint == self.remote_fingerprint:
            raise ValueError('Remote peers require distinct identities')

    def check(self):
        local = trust_state.authorize_outbound(self.trust_state_path,
            expected_scope=self.scope, role=self.local_role)
        remote = trust_state.authorize_inbound_peer(self.trust_state_path,
            expected_scope=self.scope, role=self.remote_role, fingerprint=self.remote_fingerprint)
        for authority, fingerprint in ((local, self.local_fingerprint), (remote, self.remote_fingerprint)):
            if (authority.fingerprint != fingerprint or authority.store_id != self.store_id
                    or authority.revision != self.revision or authority.epoch != self.epoch
                    or authority.scope != self.scope):
                raise PermissionError('Remote peer authority changed')

    def seal(self, value):
        self.check()
        raw = envelope.seal_envelope(encode(value), gnupg_home=self.gnupg_home,
            signer_fingerprint=self.local_fingerprint, recipient_fingerprints=(self.remote_fingerprint,),
            local_trust=LocalTrust({self.local_fingerprint}), passphrase_reference=self.passphrase_reference,
            secret_resolver=self.secret_resolver, disable_compression=True)
        self.check()
        if len(raw) > MAX_CIPHERTEXT:
            raise ValueError('Control ciphertext exceeds limit')
        return raw

    def open(self, raw):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_CIPHERTEXT:
            raise ValueError('Control ciphertext exceeds limit')
        self.check()
        payload = opening.open_envelope(raw, gnupg_home=self.gnupg_home,
            policy=EnvelopePolicy((self.remote_fingerprint,), (self.local_fingerprint,)),
            local_trust=LocalTrust({self.remote_fingerprint}), passphrase_reference=self.passphrase_reference,
            secret_resolver=self.secret_resolver)
        self.check()
        if len(payload) > MAX_PAYLOAD:
            raise ValueError('Control payload exceeds limit')
        value = _decode(payload)
        if encode(value) != payload:
            raise ValueError('Control payload is not canonical')
        return value
