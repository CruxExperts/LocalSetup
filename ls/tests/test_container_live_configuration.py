"""Container tuning is signed, bounded and separate from lifecycle authority."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core import live_configuration as config
from ls.core.live_configuration_runtime import ConfigurationRuntime
from ls.core.live_configuration_files import _pretty
from ls.core.openpgp import container_runtime as runtime
from ls.core.openpgp import container_live_configuration as live
from ls.core.openpgp.container_storage import write_private_file
from ls.core.openpgp.container_preparation import ContainerPreparationLimits
from ls.tests.test_live_configuration import storage_authority


@pytest.fixture
def configured(tmp_path, storage_authority, monkeypatch):
    authority, _signatures = storage_authority
    root = tmp_path/('a'*32)
    base = runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(root.name))
    store = config.ConfigurationStore(tmp_path/'signed', 'b'*32, authority)
    store.commit({'containers': runtime._config_dict(base)}, expected_digest=None, authorized=True, cadence_seconds=1)
    now = [0.0]
    shared = ConfigurationRuntime(store, {'containers': lambda value: runtime._parse_config(value, base.runtime_id) and None},
        actor='1'*64, machine='2'*64, operation='3'*64, clock=lambda: now[0])
    path = tmp_path/'authority.json'
    write_private_file(path, _pretty({'owners': {'containers': {'kind': 'container-runtime', 'runtime_id': root.name}},
        'secret': {'provider': 'env', 'name': 'CONFIGURATION_TEST_SECRET'}, 'root': str(store.root),
        'gnupg_home': str(tmp_path/'keys'), 'trust': {'path': str(tmp_path/'trust')}}), replace=False)
    monkeypatch.setattr(live, 'bound_runtime', lambda *a, **kw: (shared, {},
        __import__('hashlib').sha256(path.read_bytes()).hexdigest(), True))
    receipt = live.bind_configuration(root, authority_file=path, config_owner='containers',
                                     home=tmp_path, authorized=True)
    assert receipt['state'] == 'bound'
    return SimpleNamespace(root=root, base=base, shared=shared, now=now, authority=path)


def test_cadence_applies_only_tuning_and_preserves_legacy_bytes(configured):
    t = configured
    before = (t.root/'runtime.json').read_bytes()
    consumer = live.ContainerConfiguration(t.root, t.base)
    head = t.shared.accepted.digest
    changed = replace(t.base, max_scan_objects=25, max_delta_depth=8, scan_interval_seconds=120)
    t.shared.store.commit({'containers': runtime._config_dict(changed)}, expected_digest=head, authorized=True, cadence_seconds=1)
    assert consumer.poll() == t.base
    t.now[0] = 1.1
    assert consumer.poll() == changed
    assert runtime.load_runtime_config(t.root) == t.base
    assert (t.root/'runtime.json').read_bytes() == before
    assert not (t.root/'identity.json').exists()
    t.shared.store.commit({'containers': runtime._config_dict(replace(changed, enabled=True))},
        expected_digest=t.shared.accepted.digest, authorized=True, cadence_seconds=1)
    t.now[0] = 2.2
    assert consumer.poll() == changed
    assert consumer.error == 'configuration_runtime_lifecycle_required'
    assert live.environment_names(t.root) == ('CONFIGURATION_TEST_SECRET',)


def test_binding_and_legacy_drift_fail_closed(configured):
    t = configured
    consumer = live.ContainerConfiguration(t.root, t.base)
    selected = live.binding(t.root)
    runtime._write(t.root/'configuration-binding.json', selected | {'config_owner': 'changed'})
    with pytest.raises(config.ConfigurationError, match='configuration_binding_changed'):
        consumer.poll()
    runtime._write(t.root/'configuration-binding.json', selected)
    changed = runtime.configure_runtime(t.root, expected_revision=t.base.revision,
        proposed=replace(t.base, enabled=True))
    with pytest.raises(config.ConfigurationError, match='configuration_conflict'):
        live.ContainerConfiguration(t.root, changed)
    t.shared.stage('containers', runtime._config_dict(changed))
    t.shared.flush(authorized=True)
    assert live.bind_configuration(t.root, authority_file=t.authority, config_owner='containers',
        home=t.root.parent, authorized=True)['state'] == 'bound'
    with pytest.raises(config.ConfigurationError, match='configuration_binding_changed'):
        consumer.check()
    with pytest.raises(config.ConfigurationError, match='configuration_unauthorized'):
        live.bind_configuration(t.root, authority_file=t.authority, config_owner='containers',
                                home=t.root.parent, authorized=False)


def test_signed_schedule_interval_changes_at_boundary(configured, monkeypatch):
    from ls.core.openpgp import container_schedule as jobs
    t = configured
    job = {'runtime_root': str(t.root), 'runtime_sha256': 'c'*64, 'interval_seconds': 900,
           'configuration_mode': 'signed-tuning-v1'}
    monkeypatch.setattr(jobs, 'bind_job', lambda *a: dict(job))
    monkeypatch.setattr(jobs, 'ContainerConfiguration', lambda *a: SimpleNamespace(config=replace(t.base, scan_interval_seconds=60)))
    now = [1000.0]
    calls = []
    monkeypatch.setattr(jobs.time, 'time', lambda: now[0])
    monkeypatch.setattr(jobs, 'resolve_catalog', lambda *a, **kw: SimpleNamespace(
        reader=SimpleNamespace(limits=ContainerPreparationLimits()), reconcile=lambda b: calls.append(now[0])))
    assert jobs.run_due(job)['state'] == 'reconciled'
    now[0] += 59
    assert jobs.run_due(job)['state'] == 'not_due'
    now[0] += 1
    assert jobs.run_due(job)['state'] == 'reconciled'
    assert calls == [1000, 1060]
