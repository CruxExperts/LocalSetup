"""Explicit bounded incoming contexts preserve legacy configuration and trust."""
import copy
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from ls.core.openpgp import artifacts, container_runtime as runtime
from ls.core.openpgp.container_live_configuration import _fixed


def admissions(root):
    result = []
    for index in range(2):
        remote = runtime.RemoteContentTrust(root / f'trust-{index}', str(index + 1)*64, ('content',))
        policy = artifacts.ArtifactDiscoveryPolicy(remote.store_id, 4, 2, remote.scope,
            str(index + 3)*40, ('A'*40,), 'content.read', root/'reads', 1_800_000_000,
            1_800_003_600, 3600, 3600)
        result.append(runtime.ContentAdmission(remote, policy))
    return tuple(result)


def configuration(root):
    one, two = admissions(root)
    return runtime.RuntimeConfig(root.name, remote_content=one.remote_content,
        read_policy=one.read_policy, additional_admissions=(two,))


def test_configuration_roundtrip_legacy_shape_and_cas(tmp_path):
    root = tmp_path/('a'*32)
    multi = configuration(root)
    encoded = runtime._config_dict(multi)
    assert encoded['version'] == 6
    assert runtime._parse_config(encoded, root.name) == multi
    for worker in (False, True):
        legacy = replace(multi, additional_admissions=(),
                         read_policy=replace(multi.read_policy, require_worker_snapshots=worker,
                                             controller_fingerprint='A'*40 if worker else None))
        record = runtime._config_dict(legacy)
        assert record['version'] == (4 if worker else 3)
        assert 'additional_admissions' not in record
        assert runtime._parse_config(record, root.name) == legacy
    from ls.core.openpgp.container_consolidation_config import ConsolidationBinding
    for extra, version in (((), 5), (multi.additional_admissions, 6)):
        consolidated = replace(multi, additional_admissions=extra,
            consolidation=ConsolidationBinding(root/'authority.json', 'b'*64))
        record = runtime._config_dict(consolidated)
        assert record['version'] == version
        assert runtime._parse_config(record, root.name) == consolidated
    written = runtime.configure_runtime(root, expected_revision=0, proposed=multi)
    before = (root/'runtime.json').read_bytes()
    assert runtime.load_runtime_config(root) == written
    assert (root/'runtime.json').read_bytes() == before
    assert not (root/'identity.json').exists()
    with pytest.raises(runtime.RuntimeErrorCode, match='revision_conflict'):
        runtime.configure_runtime(root, expected_revision=0, proposed=multi)
    assert (root/'runtime.json').read_bytes() == before
    assert _fixed(multi) != _fixed(replace(multi, additional_admissions=()))


@pytest.mark.parametrize('change', ['empty', 'overflow', 'duplicate', 'primary', 'path',
                                    'scope', 'boundary', 'unknown', 'downgrade', 'future'])
def test_invalid_admission_sets_fail_closed(tmp_path, change):
    root = tmp_path/('a'*32)
    record = runtime._config_dict(configuration(root))
    member = record['additional_admissions'][0]
    if change == 'empty': record['additional_admissions'] = []
    elif change == 'overflow': record['additional_admissions'] *= 16
    elif change == 'duplicate': record['additional_admissions'] *= 2
    elif change == 'primary': record['read_policy'] = None
    elif change == 'path': member['remote_content']['path'] = 'relative'
    elif change == 'scope': member['remote_content']['scope'] = ['different']
    elif change == 'boundary': member['read_policy']['boundary'] = str(root/'different')
    elif change == 'unknown': member['enroll'] = True
    elif change == 'downgrade': record['version'] = 3
    elif change == 'future': record['version'] = 7
    with pytest.raises(runtime.RuntimeErrorCode, match='config_invalid'):
        runtime._parse_config(record, root.name)
    primary = runtime.ContentAdmission(configuration(root).remote_content, configuration(root).read_policy)
    with pytest.raises(runtime.RuntimeErrorCode, match='config_invalid'):
        replace(configuration(root), additional_admissions=(primary,))
    assert not list(tmp_path.iterdir())


def test_each_context_resolves_its_own_current_store_without_catalog_authority(tmp_path, monkeypatch):
    config = configuration(tmp_path/('a'*32))
    one, two = admissions(tmp_path/('a'*32))
    snapshots = {a.remote_content.path: SimpleNamespace(store_id=a.remote_content.store_id,
        revision=a.read_policy.trust_revision, epoch=a.read_policy.governance_epoch) for a in (one, two)}
    calls = []
    def load(path, *, expected_scope):
        calls.append((path, expected_scope))
        return snapshots[path]
    monkeypatch.setattr(runtime.trust_state, 'load_trust_state', load)
    local = SimpleNamespace(store_id='F'*64)
    first, additional = runtime._resolve_content_contexts(config, local)
    assert first is snapshots[one.remote_content.path]
    assert additional == (snapshots[two.remote_content.path],)
    assert calls == [(a.remote_content.path, a.remote_content.scope) for a in (one, two)]
    valid = copy.copy(additional[0])
    for key, value in (('store_id', local.store_id), ('store_id', 'E'*64), ('revision', 5), ('epoch', 3)):
        snapshots[two.remote_content.path] = copy.copy(valid)
        setattr(snapshots[two.remote_content.path], key, value)
        with pytest.raises(runtime.RuntimeErrorCode, match='remote_content_mismatch'):
            runtime._resolve_content_contexts(config, local)


def test_multi_origin_configuration_does_not_create_an_identity_on_read(tmp_path):
    from ls.core.openpgp.container_access import resolve_catalog
    from ls.core.openpgp.container_reader import ContainerReadError
    root = tmp_path/('a'*32)
    runtime.configure_runtime(root, expected_revision=0, proposed=replace(configuration(root), enabled=True))
    with pytest.raises(ContainerReadError, match='unauthorized'):
        resolve_catalog(root, deadline=time.monotonic()+10)
    assert not (root/'catalog').exists()
    assert not (root/'identity.json').exists()
