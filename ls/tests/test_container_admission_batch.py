"""Atomic reducer and encrypted catalog evidence for independent admissions."""
import copy
from dataclasses import replace

import pytest

from ls.core.openpgp import artifacts, container_reconcile as reconcile
from ls.core.openpgp.container_catalog import ADMISSION_BATCH_FEATURE, CatalogError, CatalogErrorCode
from ls.core.openpgp.container_catalog_runtime import _stable
from ls.tests.test_secure_container_reconcile import (
    _metadata, _policy, _transition, NOW, VOLUME_ONE, VOLUME_TWO,
)
from ls.tests.test_secure_container_catalog import catalog_env, keys, _transition as rejected_transition


def captures(tmp_path):
    first = _policy(tmp_path)
    second = replace(first, origin_fingerprint='D'*40, trust_store_id='2'*64)
    result = []
    for index, (policy, volume) in enumerate(((first, VOLUME_ONE), (second, VOLUME_TWO))):
        metadata = copy.deepcopy(_metadata(volume))
        metadata = replace(metadata, admission_policy_sha256=artifacts.container_admission_policy_sha256(policy))
        observation = reconcile._capture_observation(source_id='source', locator='opaque-'+str(index),
            identity=str(index+1)*64, metadata=metadata, observed_at=NOW)
        result.append(_transition(policy, (observation,), (reconcile.ScanCoverage('source', 'complete'),),
                                  scan_id='3'*32))
    return tuple(result)


def empty():
    return dict(containers=[], versions=[], sources=[], reconciliation_history=[])


def test_atomic_admissions_keep_both_observed_and_preserve_each_trust(tmp_path):
    batch = captures(tmp_path)
    first = reconcile.apply_reconcile_transition(empty(), batch[0], max_entities=100)
    sequential = reconcile.apply_reconcile_transition(first, batch[1], max_entities=100)
    assert {row['availability'] for row in sequential['containers']} == {'observed', 'not_observed'}
    combined = reconcile.apply_reconcile_batch(empty(), batch, max_entities=100)
    assert all(row['availability'] == 'observed' for row in combined['containers'])
    assert reconcile.apply_reconcile_batch(empty(), batch[::-1], max_entities=100) == combined
    assert reconcile.apply_reconcile_batch(combined, batch, max_entities=100) == combined
    history = combined['reconciliation_history']
    assert {row['trust_store_id'] for row in history} == {'1'*64, '2'*64}
    assert {row['policy_sha256'] for row in history} == {row.policy_sha256 for row in batch}
    assert len({row['admission_batch'] for row in history}) == 1
    changed = copy.deepcopy(combined)
    changed['reconciliation_history'][0]['rejected'].append(dict(source_id='source', locator='opaque',
        status='rejected', error_code='AUTHORIZATION_DENIED', observed_at=NOW))
    assert _stable(changed) != _stable(combined)
    changed = copy.deepcopy(combined)
    for row in changed['reconciliation_history']: row['evaluation_time'] += 1
    assert _stable(changed) == _stable(combined)


@pytest.mark.parametrize('mismatch', ['scan_id', 'evaluation_time', 'source_config_sha256', 'coverage'])
def test_scan_boundaries_cannot_be_spliced(tmp_path, mismatch):
    one, two = captures(tmp_path)
    options = dict(scan_id=two.scan_id, evaluation_time=two.evaluation_time,
        source_config_sha256=two.source_config_sha256, policy=replace(_policy(tmp_path),
        origin_fingerprint='D'*40, trust_store_id='2'*64), coverage=two.coverage, observations=two.observations)
    options[mismatch] = dict(scan_id='4'*32, evaluation_time=NOW+1, source_config_sha256='5'*64,
                            coverage=(reconcile.ScanCoverage('source', 'incomplete'),))[mismatch]
    other = reconcile.build_transition(**options)
    with pytest.raises(reconcile.ReconcileError):
        reconcile.apply_reconcile_batch(empty(), (one, other), max_entities=100)


def test_batch_rejects_mutated_proofs_duplicates_and_shared_work_overflow(tmp_path, monkeypatch):
    batch = captures(tmp_path)
    for values in ((), batch*9, (batch[0], batch[0])):
        with pytest.raises(reconcile.ReconcileError):
            reconcile.apply_reconcile_batch(empty(), values, max_entities=100)
    with monkeypatch.context() as bounds:
        bounds.setattr(reconcile, '_MAX_OBSERVATIONS', 1)
        with pytest.raises(reconcile.ReconcileError, match='BOUNDED'):
            reconcile.apply_reconcile_batch(empty(), batch, max_entities=100)
    with monkeypatch.context() as bounds:
        maximum = max(len(artifacts.canonical_bytes(item.to_record())) for item in batch) + 1
        bounds.setattr(reconcile, '_MAX_TRANSITION_BYTES', maximum)
        with pytest.raises(reconcile.ReconcileError, match='BOUNDED'):
            reconcile.apply_reconcile_batch(empty(), batch, max_entities=100)
    batch[1].observations[0].metadata.logical['files'][0]['length'] += 1
    with pytest.raises(reconcile.ReconcileError):
        reconcile.apply_reconcile_batch(empty(), batch, max_entities=100)


def test_one_carrier_cannot_select_between_two_admitted_contexts(tmp_path):
    one, two = captures(tmp_path)
    observed = two.observations[0]
    ambiguous = reconcile._capture_observation(source_id=observed.source_id,
        locator=one.observations[0].locator, identity=observed.identity,
        metadata=observed.metadata, observed_at=NOW)
    other = _transition(replace(_policy(tmp_path), origin_fingerprint='D'*40, trust_store_id='2'*64),
                        (ambiguous,), two.coverage, scan_id=two.scan_id)
    with pytest.raises(reconcile.ReconcileError, match='AUTHORIZATION_DENIED'):
        reconcile.apply_reconcile_batch(empty(), (one, other), max_entities=100)


def test_encrypted_batch_commit_preserves_feature_across_checkpoint_and_legacy_writes(catalog_env):
    store, context, policy = catalog_env.store, catalog_env.context, catalog_env.policy
    one = rejected_transition(policy)
    two = rejected_transition(replace(policy, origin_fingerprint='D'*40, trust_store_id='2'*64),
                              evaluation_time=one.evaluation_time)
    before = store.load(context)
    committed = store.commit(before.head, reconcile.ReconcileBatch((two, one)), context)
    assert committed.sequence == 1 and committed.required_features == (ADMISSION_BATCH_FEATURE,)
    assert store.load(context) == committed
    assert len(committed.catalog['reconciliation_history']) == 2
    record = store._open_record(store._read_object(committed.head), context)
    assert record['minor'] == 1 and record['required_features'] == [ADMISSION_BATCH_FEATURE]
    with pytest.raises(CatalogError, match='AUTHORIZATION_DENIED'):
        replace(committed, required_features=())
    unsupported = copy.deepcopy(record); unsupported['required_features'] = ['unknown-required-feature']
    with pytest.raises(CatalogError) as error: store._validate_state(unsupported)
    assert error.value.code == CatalogErrorCode.UNSUPPORTED
    future = copy.deepcopy(record); future['transition']['version'] = 2
    with pytest.raises(CatalogError) as error: store._validate_state(future)
    assert error.value.code == CatalogErrorCode.UNSUPPORTED
    objects = tuple(store.objects.iterdir())
    wrong_scan = rejected_transition(policy, scan_id='d'*32, evaluation_time=one.evaluation_time)
    with pytest.raises(CatalogError):
        store.commit(committed.head, reconcile.ReconcileBatch((wrong_scan, two)), context)
    assert store.load(context) == committed and tuple(store.objects.iterdir()) == objects
    assert store.commit(committed.head, reconcile.ReconcileBatch((one, two)), context) == committed
    checkpoint = store.checkpoint(committed.head, context)
    assert checkpoint.required_features == committed.required_features
    latest = store.commit(checkpoint.head, rejected_transition(policy, scan_id='c'*32), context)
    assert latest.required_features == committed.required_features
    assert b'sentinel-catalog-locator' not in store._read_object(latest.head)
