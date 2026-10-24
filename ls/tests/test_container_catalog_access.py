"""Reference grants and catalog views preserve explicit scope and conflicts."""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from ls.core.agent.file_grants import FileGrant
from ls.core.openpgp.container_catalog_runtime import CatalogRuntime
from ls.core.openpgp.container_catalog_view import index_rows
from ls.core.openpgp.container_index import parse_reference
from ls.core.openpgp.container_reader import ContainerReadError


def test_indexed_references_require_explicit_read_and_disclosure(tmp_path):
    artifact, version, member = 'a'*32, 'b'*32, 'c'*32
    name = f'@artifacts/{artifact}/{version}/@files/{member}'
    grant = FileGrant('t', 's', tmp_path, ('.',), ('.',), ('.',), time.monotonic()+10)
    with pytest.raises(PermissionError, match='outside'):
        grant.check('t', 's', 'read', name)
    scoped = replace(grant, read=(f'@artifacts/{artifact}',))
    scoped.check('t', 's', 'read', name)
    with pytest.raises(PermissionError, match='disclosure'):
        scoped.check('t', 's', 'read', name, provider=True)
    allowed = replace(scoped, disclose=(name,))
    allowed.check('t', 's', 'read', name, provider=True)
    with pytest.raises(PermissionError, match='read-only'):
        replace(allowed, write=('@artifacts',)).check('t', 's', 'write', name)
    assert parse_reference(name) == (artifact, version, member)
    for malformed in ('@artifacts/..', '@artifacts/'+artifact+'/', name+'/extra'):
        with pytest.raises(ContainerReadError):
            parse_reference(malformed)


def test_catalog_views_preserve_forks_and_refuse_collision_winners():
    artifact, base, left, right = ('a'*32, 'b'*32, 'c'*32, 'd'*32)
    def row(version, parents, commitment):
        return {'artifact_id': artifact, 'version_id': version, 'parents': parents,
                'commitment_sha256': commitment, 'claim_conflict': False}
    rows = [row(base, [], '1'*64), row(left, [base], '2'*64), row(right, [base], '3'*64)]
    def result(values):
        return SimpleNamespace(snapshot=SimpleNamespace(catalog={'containers': values}), observations=())
    forward = list(index_rows(result(rows), None))
    reverse = list(index_rows(result(list(reversed(rows))), None))
    assert forward == reverse
    assert forward[1]['heads'] == [left, right] and forward[1]['state'] == 'conflict'
    rows.append(row(left, [base], '4'*64))
    collision = result(rows)
    with pytest.raises(ContainerReadError, match='conflict'):
        CatalogRuntime._collision(collision, artifact, left)
    versions = list(index_rows(collision, artifact))
    assert any(item.get('path', '').endswith(left) and item['state'] == 'conflict' for item in versions)
    cyclic = result([row(left, [right], '2'*64), row(right, [left], '3'*64)])
    with pytest.raises(ContainerReadError, match='conflict'):
        CatalogRuntime._collision(cyclic, artifact, left)
    assert all(row['state'] == 'conflict' for row in list(index_rows(cyclic, artifact))[1:])


def test_scan_stops_retaining_observations_at_the_catalog_byte_bound(tmp_path, monkeypatch):
    from ls.core.openpgp import container_catalog_runtime as module
    from ls.core.openpgp.artifact_records import canonical_bytes
    from ls.core.openpgp.container_preparation import _Budget, ContainerPreparationLimits
    from ls.core.openpgp.container_reconcile import _observation_record
    from ls.core.openpgp.container_scan import LocalCandidate, LocalInventory
    from ls.core.openpgp.container_sources import LocalFileRangeSource
    from ls.tests.test_secure_container_reconcile import _observation, _policy, VOLUME_ONE, VOLUME_TWO
    root = tmp_path/('a'*32); root.mkdir(mode=0o700)
    observations = [_observation(str(index)*64, 'nas', str(index), volume)
                    for index, volume in ((1, VOLUME_ONE), (2, VOLUME_TWO))]
    candidates = []
    for index in range(3):
        path = root/str(index); path.write_bytes(b'candidate')
        with LocalFileRangeSource(path) as source:
            candidates.append(LocalCandidate('nas', str(index), path, source.generation))
    private_reader = SimpleNamespace(check=lambda: None, policy=_policy(root), max_sources=10,
        trust_state_path=root/'trust', gnupg_home=root/'keys', passphrase_reference=None, secret_resolver=None)
    config = SimpleNamespace(runtime_id=root.name, source_ids=('nas',), source_directories=(root,), max_scan_objects=10)
    service = CatalogRuntime(root, config, None, private_reader)
    monkeypatch.setattr(service.store, 'limits', SimpleNamespace(
        transaction_bytes=len(canonical_bytes(_observation_record(observations[0])))+1))
    monkeypatch.setattr(service.store, 'load', lambda _: SimpleNamespace(head=None,
        catalog={'containers': [], 'versions': [], 'sources': [], 'reconciliation_history': []}))
    monkeypatch.setattr(service.store, '_anchor', lambda _: SimpleNamespace(pending_head=None))
    monkeypatch.setattr(module, 'scan_directories', lambda *_a, **_k: LocalInventory(tuple(candidates), (('nas', 'complete'),)))
    calls = []
    def inspect(*_a, **_k):
        calls.append(True)
        assert len(calls) <= 2, 'inspection continued after aggregate limit'
        return observations[len(calls)-1]
    monkeypatch.setattr(module, 'inspect_candidate', inspect)
    monkeypatch.setattr(service.store, 'commit', lambda *_a, **_k: pytest.fail('oversized scan published'))
    with pytest.raises(ContainerReadError, match='limit_exceeded'):
        service.reconcile(_Budget(ContainerPreparationLimits()))
    assert len(calls) == 2


def test_pending_catalog_write_stops_discovery_and_new_publication(tmp_path, monkeypatch):
    from ls.core.openpgp import container_catalog_runtime as module
    root = tmp_path/('a'*32)
    root.mkdir(mode=0o700)
    service = CatalogRuntime(root, SimpleNamespace(runtime_id=root.name), None,
                             SimpleNamespace(check=lambda: None))
    monkeypatch.setattr(service.store, 'load', lambda _context: pytest.fail('pending catalog was consumed'))
    monkeypatch.setattr(service.store, '_anchor', lambda _context: SimpleNamespace(pending_head='b'*64))
    monkeypatch.setattr(module, 'scan_directories', lambda *_a, **_k: pytest.fail('pending write was replayed'))
    monkeypatch.setattr(service.store, 'commit', lambda *_a, **_k: pytest.fail('pending write was replayed'))
    for _ in range(2):
        with pytest.raises(ContainerReadError, match='pending'):
            service.reconcile(SimpleNamespace(checkpoint=lambda: None))
