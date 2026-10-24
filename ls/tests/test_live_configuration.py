"""Atomic owner sets, explicit signature authority, conflict and crash recovery."""
from dataclasses import replace
import hashlib

import pytest

from ls.core import live_configuration as config
from ls.core.live_configuration_runtime import ConfigurationRuntime
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


@pytest.fixture
def storage_authority(tmp_path, monkeypatch):
    """Use a clearly synthetic envelope only for filesystem transaction tests."""
    calls = []
    authority = config.ConfigurationAuthority('A'*40, ('A'*40,), tmp_path/'keys',
        SecretReference(SecretProvider.ENV, 'TEST_CONFIG_KEY'), SecretResolver(env_values={}), lambda: None)
    def seal(self, payload):
        self.check()
        calls.append(payload)
        return b'fixture-envelope:' + payload
    def open_(self, serialized):
        self.check()
        if not serialized.startswith(b'fixture-envelope:'):
            raise config.ConfigurationError('fixture_signature_invalid')
        return serialized[len(b'fixture-envelope:'):]
    monkeypatch.setattr(config.ConfigurationAuthority, 'seal', seal)
    monkeypatch.setattr(config.ConfigurationAuthority, 'open', open_)
    return authority, calls


def test_complete_sets_compare_revisions_and_recover_without_signing(tmp_path, storage_authority, monkeypatch):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'state', '1'*32, authority)
    initial = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    first = store.commit(initial, expected_digest=None, authorized=True,
                         extensions={'future': {'enabled': False}})
    assert first.revision == 1 and first.record['cadence_seconds'] == 60
    changed = first.record
    changed['values']['terminal']['minimum_rows'] = 999
    assert first.record['values'] == initial
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        store.commit(initial, expected_digest=None, authorized=True)
    assert len(signatures) == 1

    real_write = config.write_private_file
    def crash_before_head(path, *args, **kwargs):
        if path.name == 'HEAD':
            raise OSError('simulated interruption')
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(config, 'write_private_file', crash_before_head)
    next_values = {'terminal': {'minimum_rows': 12}, 'containers': {'mode': 'client_driven'}}
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
        store.commit(next_values, expected_digest=first.digest, authorized=True, cadence_seconds=1)
    candidate = failed.value.candidate
    candidate_bytes = (store.root/'objects'/candidate).read_bytes()
    assert store.load().record['values'] == initial
    with pytest.raises(config.ConfigurationError, match='configuration_pending'):
        store.commit(next_values, expected_digest=first.digest, authorized=True)
    assert len(signatures) == 2
    monkeypatch.setattr(config, 'write_private_file', real_write)
    second = store.recover(authorized=True)
    assert second.digest == candidate and second.revision == 2
    assert second.record['values'] == next_values and second.record['cadence_seconds'] == 1
    assert second.record['extensions'] == first.record['extensions']
    assert (store.root/'objects'/candidate).read_bytes() == candidate_bytes and len(signatures) == 2

    def lost_ack(path, *args, **kwargs):
        real_write(path, *args, **kwargs)
        if path.name == 'HEAD':
            raise OSError('lost publication acknowledgement')
    monkeypatch.setattr(config, 'write_private_file', lost_ack)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
        store.commit(initial, expected_digest=second.digest, authorized=True, cadence_seconds=3600)
    third = store.load(minimum_revision=2)
    assert third.revision == 3 and third.digest == failed.value.candidate
    monkeypatch.setattr(config, 'write_private_file', real_write)
    assert store.recover(authorized=True).digest == third.digest and len(signatures) == 3
    assert store.load().record['values'] == initial
    # An older valid signed revision is not current just because it still verifies.
    real_write(store.root/'HEAD', config._encode({'revision': 1, 'digest': first.digest}), replace=True)
    with pytest.raises(config.ConfigurationError, match='configuration_rollback'):
        store.load(minimum_revision=third.revision)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        store.recover(authorized=True)
    fork = first.record
    fork['values']['terminal']['minimum_rows'] = 19
    fork_bytes = authority.seal(config._encode(fork))
    fork_digest = hashlib.sha256(fork_bytes).hexdigest()
    real_write(store.root/'objects'/fork_digest, fork_bytes, replace=False)
    real_write(store.root/'HEAD', config._encode({'revision': 1, 'digest': fork_digest}), replace=True)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        store.load(minimum_revision=1, ancestor_digest=first.digest)


def test_candidate_survives_intent_failure_and_can_be_explicitly_recovered(tmp_path, storage_authority, monkeypatch):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'state', '2'*32, authority)
    real_write = config.write_private_file
    def fail_intent(path, *args, **kwargs):
        if path.name == 'intent':
            raise OSError('intent persistence failed')
        real_write(path, *args, **kwargs)
    monkeypatch.setattr(config, 'write_private_file', fail_intent)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain') as failed:
        store.commit({'terminal': {'minimum_rows': 10}}, expected_digest=None, authorized=True)
    assert store.load() is None and len(signatures) == 1
    monkeypatch.setattr(config, 'write_private_file', real_write)
    restored = store.recover(authorized=True, candidate_digest=failed.value.candidate)
    assert restored.revision == 1 and len(signatures) == 1


@pytest.mark.parametrize('cadence', [True, 0, 3601, 1.5])
def test_invalid_values_never_sign_or_publish(tmp_path, storage_authority, cadence):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'state', '3'*32, authority)
    with pytest.raises(config.ConfigurationError, match='configuration_invalid'):
        store.commit({'terminal': {}}, expected_digest=None, authorized=True, cadence_seconds=cadence)
    assert signatures == [] and store.load() is None
    denied = config.ConfigurationStore(tmp_path/'denied', '4'*32,
        replace(authority, current_check=lambda: False))
    with pytest.raises(config.ConfigurationError, match='configuration_unauthorized'):
        denied.commit({'terminal': {}}, expected_digest=None, authorized=True)
    assert not denied.root.exists()


def test_real_signature_pins_exact_complete_configuration(tmp_path, separate_machine_env):
    env = separate_machine_env
    recipients = tuple(sorted((env.owner, env.publisher, env.recipient)))
    authority = config.ConfigurationAuthority(env.owner, recipients, env.homes['controller'],
        SECRET_REFERENCE, env.resolver, lambda: None)
    store = config.ConfigurationStore(tmp_path/'protected-config', '5'*32, authority)
    values = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    snapshot = store.commit(values, expected_digest=None, authorized=True)
    envelope = (store.root/'objects'/snapshot.digest).read_bytes()
    assert b'minimum_rows' not in envelope and b'ad_hoc' not in envelope
    assert hashlib.sha256(envelope).hexdigest() == snapshot.digest
    reader = config.ConfigurationStore(store.root, store.store_id,
        replace(authority, home=env.homes['receiver']))
    assert reader.load().record['values'] == values
    # Possessing another recipient key does not authorize that key as signer.
    wrong = config.ConfigurationStore(store.root, store.store_id,
        replace(authority, signer=env.publisher, home=env.homes['origin']))
    with pytest.raises(config.ConfigurationError, match='configuration_verification_failed'):
        wrong.load()
    assert (store.root/'objects'/snapshot.digest).read_bytes() == envelope
    altered = snapshot.record
    altered['required'].append('unknown-required-feature')
    unsupported = authority.seal(config._encode(altered))
    digest = hashlib.sha256(unsupported).hexdigest()
    config.write_private_file(store.root/'objects'/digest, unsupported, replace=False)
    config.write_private_file(store.root/'HEAD', config._encode({'revision': 1, 'digest': digest}), replace=True)
    with pytest.raises(config.ConfigurationError, match='configuration_unsupported'):
        reader.load()
    assert snapshot.record['values'] == values  # Last accepted immutable value survives rejection.


def validators():
    def terminal(value):
        rows = value.get('minimum_rows')
        if type(rows) is not int or not 10 <= rows <= 100:
            raise ValueError
    def containers(value):
        if value.get('mode') not in {'ad_hoc', 'client_driven', 'designated_worker'}:
            raise ValueError
    return {'terminal': terminal, 'containers': containers}


def test_runtime_uses_one_cadence_preserves_external_edits_and_owner_set(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'runtime', '6'*32, authority)
    values = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'},
              'future-owner': {'optional': True}}
    first = store.commit(values, expected_digest=None, authorized=True)
    now = [0.0]
    runtime = ConfigurationRuntime(store, validators(), actor='1'*64, machine='2'*64,
                                   operation='3'*64, clock=lambda: now[0])
    proposed = {'minimum_rows': 12}
    runtime.stage('terminal', proposed)
    proposed['minimum_rows'] = 999
    runtime.stage_cadence(1)
    now[0] = 59
    assert runtime.poll(authorized=True) is False and len(signatures) == 1
    now[0] = 60
    assert runtime.poll(authorized=True) is True and len(signatures) == 2
    assert runtime.value('terminal') == {'minimum_rows': 12} and runtime.cadence == 1
    assert runtime.accepted.record['values']['future-owner'] == {'optional': True}
    assert runtime.accepted.record['provenance'] == runtime.provenance
    assert store.load(ancestor_digest=first.digest, minimum_revision=1).digest == runtime.accepted.digest
    external = dict(values, terminal={'minimum_rows': 18})
    second = store.commit(external, expected_digest=runtime.accepted.digest, authorized=True)
    now[0] = 61
    assert runtime.poll() is True and runtime.value('terminal') == {'minimum_rows': 18}
    runtime.stage('terminal', {'minimum_rows': 14})
    third = store.commit(dict(values, terminal={'minimum_rows': 20}), expected_digest=second.digest, authorized=True)
    before = len(signatures)
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        runtime.flush(authorized=True)
    assert len(signatures) == before and runtime.dirty and runtime.value('terminal') == {'minimum_rows': 18}
    assert store.load().digest == third.digest
    runtime.discard_staged()
    assert runtime.value('terminal') == {'minimum_rows': 20} and not runtime.dirty
    # Authenticated but invalid owner settings do not replace the accepted set.
    store.commit(dict(values, terminal={'minimum_rows': 1}), expected_digest=third.digest, authorized=True)
    with pytest.raises(config.ConfigurationError, match='configuration_invalid'):
        runtime.reload()
    assert runtime.accepted.digest == third.digest
    assert runtime.value('terminal') == {'minimum_rows': 20}
    assert all('values' not in row and 'minimum_rows' not in str(row) for row in runtime.events)


def test_runtime_recovers_uncertainty_without_resigning_or_losing_proposals(tmp_path, storage_authority, monkeypatch):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'runtime', '7'*32, authority)
    values = {'terminal': {'minimum_rows': 10}, 'containers': {'mode': 'ad_hoc'}}
    first = store.commit(values, expected_digest=None, authorized=True)
    runtime = ConfigurationRuntime(store, validators(), actor='1'*64, machine='2'*64, operation='3'*64)
    runtime.stage('terminal', {'minimum_rows': 15})
    real_write = config.write_private_file
    def fail_head(path, *args, **kwargs):
        if path.name == 'HEAD':
            raise OSError('synthetic failure')
        real_write(path, *args, **kwargs)
    monkeypatch.setattr(config, 'write_private_file', fail_head)
    with pytest.raises(config.ConfigurationError, match='configuration_uncertain'):
        runtime.flush(authorized=True)
    assert runtime.accepted.digest == first.digest and runtime.uncertain is not None and runtime.dirty
    with pytest.raises(config.ConfigurationError, match='configuration_pending'):
        runtime.stage('terminal', {'minimum_rows': 16})
    with pytest.raises(config.ConfigurationError, match='configuration_pending'):
        runtime.discard_staged()
    monkeypatch.setattr(config, 'write_private_file', real_write)
    runtime.flush(authorized=True)
    assert runtime.value('terminal') == {'minimum_rows': 15}
    assert not runtime.dirty and runtime.uncertain is None and len(signatures) == 2


def test_first_runtime_flush_requires_complete_set_and_separate_write_authority(tmp_path, storage_authority):
    authority, signatures = storage_authority
    store = config.ConfigurationStore(tmp_path/'new-runtime', '8'*32, authority)
    runtime = ConfigurationRuntime(store, validators(), actor='1'*64, machine='2'*64, operation='3'*64)
    runtime.stage('terminal', {'minimum_rows': 10})
    with pytest.raises(config.ConfigurationError, match='configuration_incomplete'):
        runtime.flush(authorized=True)
    runtime.stage('containers', {'mode': 'ad_hoc'})
    with pytest.raises(config.ConfigurationError, match='configuration_unauthorized'):
        runtime.flush(authorized=False)
    assert signatures == [] and not store.root.exists() and runtime.accepted is None
    runtime.flush(authorized=True)
    assert len(signatures) == 1 and runtime.accepted.revision == 1 and not runtime.dirty
