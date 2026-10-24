"""Independent receipt stores remain locked and separate through file release."""
from dataclasses import replace
from contextlib import contextmanager
import hashlib
import sqlite3

import pytest

from ls.core.openpgp import trust_state as trust
from ls.core.openpgp.container_dependency_receipts import DependencyReceipt
from ls.tests.test_openpgp_trust_state import store, SCOPE, PUBLISHER


@pytest.fixture
def receipts(store):
    path, clock, initial = store
    other = path.parent/'other.db'
    second = trust.initialize_trust_state(other, scope=SCOPE,
        owner_certificate=b'owner', publisher_certificate=b'publisher')
    records = []
    for selected, snapshot, digit in ((path, initial, '1'), (other, second, '2')):
        layers = ((('metadata-'+digit).encode(), 'publisher', PUBLISHER),)
        arguments = dict(expected_store_id=snapshot.store_id, expected_revision=snapshot.revision,
            expected_epoch=snapshot.epoch, expected_scope=SCOPE, created_at=clock[0]-1,
            expires_at=clock[0]+100, release_payload=lambda: None, rollback_payload=lambda: None)
        _, metadata = trust._record_accepted_container(selected, identity_prefix=digit*32,
            metadata_identity=None, layers=layers, **arguments)
        _, message = trust._record_accepted_container(selected, identity_prefix=digit*32+':0',
            metadata_identity=metadata, layers=(*layers, (('message-'+digit).encode(), 'publisher', PUBLISHER)),
            **arguments)
        records.append((selected, snapshot, layers, arguments, metadata, message))
    selected, snapshot, layers, arguments, metadata, message = records[1]
    dependency = DependencyReceipt(selected, metadata, (message,),
        tuple((hashlib.sha256(data).hexdigest(), role, signer) for data, role, signer in layers),
        snapshot.store_id, snapshot.revision, snapshot.epoch, SCOPE, clock[0]-1, clock[0]+50)
    def release(*, dependencies=(dependency,), publish=lambda: None, rollback=lambda: None):
        selected, _, layers, arguments, metadata, message = records[0]
        return trust._record_accepted_container(selected, identity_prefix='3'*32,
            metadata_identity=metadata, layers=layers, required_selected_receipts=(message,),
            dependency_receipts=dependencies, **(arguments | dict(release_payload=publish, rollback_payload=rollback)))
    return path, other, clock, dependency, release


def test_release_holds_both_stores_without_copying_receipts(receipts):
    path, other, _, dependency, release = receipts
    observations = []
    def publish():
        for selected in (path, other):
            with sqlite3.connect(selected, timeout=0) as db:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    db.execute('UPDATE state SET revision=revision+1 WHERE singleton=1')
        observations.append(True)
    assert release(publish=publish)[0] == 'accepted'
    assert release(publish=publish)[0] == 'already_accepted'
    assert observations == [True, True]
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT 1 FROM consumed WHERE value=?', (dependency.selected_receipts[0],)).fetchone() is None
        assert db.execute("SELECT count(*) FROM consumed WHERE kind='container-file-v1'").fetchone()[0] == 1
    with sqlite3.connect(other) as db:
        assert db.execute("SELECT count(*) FROM consumed WHERE kind='container-file-v1'").fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['store', 'revision', 'scope', 'signer', 'metadata', 'selected', 'layer', 'expiry', 'age'])
def test_invalid_dependency_cannot_release_or_record_file(receipts, fault):
    path, other, clock, dependency, release = receipts
    changes = {
        'store': dict(expected_store_id='f'*64), 'revision': dict(expected_revision=99),
        'scope': dict(expected_scope=('different',)),
        'signer': dict(layers=((dependency.layers[0][0], 'publisher', 'C'*40),)),
        'metadata': dict(metadata_identity='f'*64), 'selected': dict(selected_receipts=('f'*64,)),
        'layer': dict(layers=(('f'*64, 'publisher', PUBLISHER),)),
        'expiry': dict(expires_at=clock[0]),
        'age': dict(admit_from=clock[0]-10, admit_until=clock[0]+100,
                    max_content_age_seconds=1, max_content_lifetime_seconds=10),
    }
    with pytest.raises(trust.TrustStateError):
        release(dependencies=(replace(dependency, **changes[fault]),),
                publish=lambda: pytest.fail('unverified dependency released'))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM consumed WHERE kind='container-file-v1'").fetchone() is None


def test_expiry_during_release_rolls_back_payload_and_primary_receipt(receipts):
    path, _, clock, _, release = receipts
    output = []
    def publish():
        output.append('published')
        clock[0] += 60
    with pytest.raises(trust.TrustStateError, match='stale dependency'):
        release(publish=publish, rollback=output.clear)
    assert output == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM consumed WHERE kind='container-file-v1'").fetchone() is None


def test_contended_dependency_fails_without_publication_or_retry(receipts):
    path, other, _, _, release = receipts
    with sqlite3.connect(other) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            release(publish=lambda: pytest.fail('contended dependency released'))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM consumed WHERE kind='container-file-v1'").fetchone() is None


def test_dependency_bounds_and_invalid_shapes_are_rejected(receipts):
    _, _, _, dependency, release = receipts
    for value in ([dependency], (dependency,)*65, (object(),)):
        with pytest.raises(trust.TrustStateError):
            release(dependencies=value)
    for changes in (dict(selected_receipts=()), dict(expected_epoch=True),
                    dict(expected_scope=['scope']), dict(layers=(('bad', 'publisher', PUBLISHER),))):
        with pytest.raises(trust.TrustStateError):
            replace(dependency, **changes)


def test_revoked_dependency_signer_cannot_reuse_existing_receipts(receipts):
    path, other, clock, _, release = receipts
    with sqlite3.connect(other) as db:
        db.execute('INSERT INTO revocations VALUES (?,?,?)', ('publisher', PUBLISHER, clock[0]))
    with pytest.raises(trust.TrustStateError, match='revoked'):
        release(publish=lambda: pytest.fail('revoked dependency released'))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM consumed WHERE kind='container-file-v1'").fetchone() is None


def test_canonical_order_opens_a_shared_dependency_store_once(receipts, monkeypatch):
    path, other, _, dependency, release = receipts
    opened = []
    original = trust._open
    @contextmanager
    def observe(selected):
        opened.append(selected)
        with original(selected) as db:
            yield db
    monkeypatch.setattr(trust, '_open', observe)
    assert release(dependencies=(dependency, dependency))[0] == 'accepted'
    assert opened == sorted((path, other))
