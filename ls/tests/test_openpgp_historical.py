"""Persisted receipt-bound historical opening never relaxes ordinary reads."""
from __future__ import annotations

import pytest

from ls.core.openpgp import trust_state
from ls.core.openpgp.historical import open_historical_envelope
from ls.core.openpgp.opening import open_envelope, EnvelopeOpenError
from ls.core.openpgp.contracts import LocalTrust
from ls.tests.test_openpgp_opening import _fake_gpg, _inner_message, _outer_envelope, _policy, _status, SIGNER
from ls.tests.test_openpgp_trust_state import store, SCOPE


@pytest.mark.parametrize('status', [b'EXPSIG', b'EXPKEYSIG', b'REVKEYSIG'])
def test_expired_or_revoked_signature_requires_exact_persisted_receipt(store, tmp_path, monkeypatch, status):
    path, _, snapshot = store
    cipher = _outer_envelope()
    content = b'historical\x00bytes\xff'
    home, _, calls = _fake_gpg(tmp_path, monkeypatch, plaintext=_inner_message(content),
                               diagnostics=_status().replace(b'GOODSIG', status))
    arguments = dict(trust_state_path=path, expected_scope=SCOPE, role='owner',
                     signer_fingerprint=SIGNER, gnupg_home=home, policy=_policy())
    with pytest.raises(trust_state.TrustStateError, match='receipt'):
        open_historical_envelope(cipher, **arguments)
    assert not calls.exists()
    with pytest.raises(EnvelopeOpenError):
        open_envelope(cipher, gnupg_home=home, policy=_policy(), local_trust=LocalTrust({SIGNER}))
    # Fixture models a trusted consumer's previously successful full open.
    trust_state.record_accepted_content(path, cipher, signer_fingerprint=SIGNER, role='owner',
        expected_scope=SCOPE, expected_store_id=snapshot.store_id, expected_revision=snapshot.revision)
    assert open_historical_envelope(cipher, **arguments) == content
    count = calls.read_bytes()
    with pytest.raises(trust_state.TrustStateError, match='receipt'):
        open_historical_envelope(cipher+b'changed', **arguments)
    assert calls.read_bytes() == count


@pytest.mark.parametrize('bad_status', [b'BADSIG', b'ERRSIG', b'NO_PUBKEY'])
def test_historical_receipt_never_permits_invalid_signature(store, tmp_path, monkeypatch, bad_status):
    path, _, snapshot = store
    cipher = _outer_envelope()
    home, _, _ = _fake_gpg(tmp_path, monkeypatch, plaintext=_inner_message(b'not released'),
                           diagnostics=_status().replace(b'GOODSIG', bad_status))
    trust_state.record_accepted_content(path, cipher, signer_fingerprint=SIGNER, role='owner',
        expected_scope=SCOPE, expected_store_id=snapshot.store_id, expected_revision=snapshot.revision)
    with pytest.raises(EnvelopeOpenError):
        open_historical_envelope(cipher, trust_state_path=path, expected_scope=SCOPE, role='owner',
            signer_fingerprint=SIGNER, gnupg_home=home, policy=_policy())


def test_historical_receipt_still_requires_integrity_and_exact_participants(store, tmp_path, monkeypatch):
    path, _, snapshot = store
    cipher = _outer_envelope()
    home, _, _ = _fake_gpg(tmp_path, monkeypatch, plaintext=_inner_message(b'not released'),
                           diagnostics=_status(good_mdc=False).replace(b'GOODSIG', b'EXPKEYSIG'))
    trust_state.record_accepted_content(path, cipher, signer_fingerprint=SIGNER, role='owner',
        expected_scope=SCOPE, expected_store_id=snapshot.store_id, expected_revision=snapshot.revision)
    arguments = dict(trust_state_path=path, expected_scope=SCOPE, role='owner',
                     signer_fingerprint=SIGNER, gnupg_home=home)
    with pytest.raises(EnvelopeOpenError):
        open_historical_envelope(cipher, policy=_policy(), **arguments)
    with pytest.raises(EnvelopeOpenError):
        open_historical_envelope(cipher, policy=_policy(signer='D'*40), **arguments)
