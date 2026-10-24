"""Hold independently owned dependency receipts during container file release."""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
import re

from . import trust_state as trust
from .contracts import normalize_fingerprint


@dataclass(frozen=True, slots=True)
class DependencyReceipt:
    """Trusted caller's exact receipt selection, never a bearer authorization."""

    path: Path
    metadata_identity: str
    selected_receipts: tuple[str, ...]
    layers: tuple[tuple[str, str, str], ...]
    expected_store_id: str
    expected_revision: int
    expected_epoch: int
    expected_scope: tuple[str, ...]
    created_at: int
    expires_at: int
    admit_from: int | None = None
    admit_until: int | None = None
    max_content_age_seconds: int | None = None
    max_content_lifetime_seconds: int | None = None

    def __post_init__(self):
        try:
            if (not isinstance(self.path, Path) or not self.path.is_absolute()
                    or '..' in self.path.parts
                    or type(self.metadata_identity) is not str
                    or re.fullmatch(r'[0-9a-f:]{1,1024}', self.metadata_identity) is None
                    or type(self.selected_receipts) is not tuple or not self.selected_receipts
                    or len(self.selected_receipts) > 1_000_000
                    or any(type(item) is not str or re.fullmatch(r'[0-9a-f:]{1,1024}', item) is None
                           for item in self.selected_receipts)
                    or type(self.layers) is not tuple or not 1 <= len(self.layers) <= 3
                    or type(self.expected_store_id) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', self.expected_store_id) is None
                    or type(self.expected_revision) is not int or self.expected_revision < 0
                    or type(self.expected_epoch) is not int or self.expected_epoch < 1
                    or type(self.expected_scope) is not tuple or not self.expected_scope
                    or len(self.expected_scope) > 32
                    or self.expected_scope != tuple(sorted(set(self.expected_scope)))
                    or any(type(item) is not str or not item or len(item.encode()) > 128
                           for item in self.expected_scope)
                    or type(self.created_at) is not int or type(self.expires_at) is not int
                    or not 0 <= self.created_at < self.expires_at):
                raise ValueError
            for layer in self.layers:
                if (type(layer) is not tuple or len(layer) != 3
                        or type(layer[0]) is not str or re.fullmatch(r'[0-9a-f]{64}', layer[0]) is None
                        or layer[1] not in ('owner', 'publisher')
                        or normalize_fingerprint(layer[2]) != layer[2]):
                    raise ValueError
            bounds = (self.admit_from, self.admit_until,
                      self.max_content_age_seconds, self.max_content_lifetime_seconds)
            if any(item is not None for item in bounds) and (
                    not all(type(item) is int for item in bounds)
                    or not 0 <= self.admit_from < self.admit_until
                    or self.max_content_age_seconds <= 0 or self.max_content_lifetime_seconds <= 0):
                raise ValueError
        except Exception:
            raise trust.TrustStateError('invalid dependency receipt') from None


def _validate_locked(db, item, now, checkpoint):
    state = trust._observe(db, now)
    trust._require_scope(state, item.expected_scope)
    state = trust._activate_due(db, now)
    if ((state['store_id'], state['revision'], state['epoch']) !=
            (item.expected_store_id, item.expected_revision, item.expected_epoch)
            or not item.created_at <= now < item.expires_at):
        raise trust.TrustStateError('stale dependency authority')
    if item.admit_from is not None and not (
            item.admit_from <= item.created_at <= now < item.admit_until
            and now-item.created_at <= item.max_content_age_seconds
            and item.expires_at-item.created_at <= item.max_content_lifetime_seconds):
        raise trust.TrustStateError('dependency outside discovery admission')
    for digest, role, signer in item.layers:
        if state[role+'_fp'] != signer:
            raise trust.TrustStateError('dependency signer is not current')
        trust._accepted_receipt_authority(db, state, now, role=role, signer=signer)
        row = db.execute('SELECT signer_fp,role FROM receipts WHERE ciphertext_sha256=?', (digest,)).fetchone()
        if row is None or (row['signer_fp'], row['role']) != (signer, role):
            raise trust.TrustStateError('dependency layer receipt missing')
    if db.execute("SELECT 1 FROM consumed WHERE kind='container-meta-v1' AND value=?",
                  (item.metadata_identity,)).fetchone() is None:
        raise trust.TrustStateError('dependency metadata receipt missing')
    for identity in item.selected_receipts:
        if checkpoint is not None:
            checkpoint()
        if db.execute("SELECT 1 FROM consumed WHERE kind='container-selected-v1' AND value=?",
                      (identity,)).fetchone() is None:
            raise trust.TrustStateError('dependency selected receipt missing')


@contextmanager
def acceptance_transaction(path, dependencies, checkpoint, *, selected_count=0):
    """Acquire stores in canonical order; preserve the legacy one-store path."""
    if type(dependencies) is not tuple or len(dependencies) > 64 or any(
            type(item) is not DependencyReceipt for item in dependencies):
        raise trust.TrustStateError('invalid dependency receipts')
    if not dependencies:
        with trust._open(path) as db:
            state, now = trust._begin_fresh(db)
            yield db, state, now
        return
    if selected_count + sum(len(item.selected_receipts) for item in dependencies) > 1_000_000:
        raise trust.TrustStateError('dependency receipt limit')
    primary = trust._path(path).resolve()
    paths = {primary, *(trust._path(item.path).resolve() for item in dependencies)}
    if len(paths) > 16:
        raise trust.TrustStateError('dependency store limit')
    with ExitStack() as stack:
        databases = {}
        for selected in sorted(paths):
            if checkpoint is not None:
                checkpoint()
            db = stack.enter_context(trust._open(selected))
            # Contention is an explicit failed attempt, never a cross-store wait
            # cycle or an automatic retry of an uncertain release.
            db.execute('PRAGMA busy_timeout=0')
            trust._begin_fresh(db)
            databases[selected] = db
        now = trust._clock()
        for item in dependencies:
            _validate_locked(databases[item.path.resolve()], item, now, checkpoint)
        db = databases[primary]
        yield db, trust._observe(db, now), now
        # Scheduled transitions or expiry may become due while the callback runs.
        now = trust._clock()
        for item in dependencies:
            _validate_locked(databases[item.path.resolve()], item, now, checkpoint)
