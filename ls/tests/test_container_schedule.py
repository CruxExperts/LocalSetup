"""Periodic indexing reuses durable admission, interval and overlap boundaries."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from ls.core.openpgp import container_schedule as jobs
from ls.core.openpgp.container_preparation import ContainerPreparationLimits
from ls.core.openpgp.container_reader import ContainerReadError
from ls.core.terminal_ops import command_scheduler as scheduler
from ls.tests.test_shell_scheduler import FakeCrontab, SOURCE_ROOT


@pytest.fixture
def configured_job(tmp_path, monkeypatch):
    root = tmp_path / ('a'*32)
    root.mkdir(mode=0o700)
    job = {'runtime_root': str(root), 'runtime_sha256': 'b'*64, 'interval_seconds': 120}
    monkeypatch.setattr(jobs, 'bind_job', lambda *_: dict(job))
    return root, job


def test_due_interval_restart_and_pending_write_are_not_replayed(configured_job, monkeypatch):
    root, job = configured_job
    clock = [1000.0]
    calls = []
    monkeypatch.setattr(jobs.time, 'time', lambda: clock[0])
    def scan(_):
        calls.append(clock[0])
        if len(calls) == 2:
            raise ContainerReadError('pending')
    monkeypatch.setattr(jobs, 'resolve_catalog', lambda *_a, **_k: SimpleNamespace(
        reader=SimpleNamespace(limits=ContainerPreparationLimits()), reconcile=scan))
    assert jobs.run_due(job)['state'] == 'reconciled'
    assert jobs.run_due(dict(job))['state'] == 'not_due'
    clock[0] += 120
    assert jobs.run_due(job)['state'] == 'pending'
    assert jobs.run_due(job)['state'] == 'not_due'
    assert calls == [1000, 1120]
    assert jobs._receipt(root)['state'] == 'pending'
    clock[0] = 999
    assert jobs.run_due(job)['state'] == 'clock_reversed'
    assert len(calls) == 2


def test_started_receipt_and_overlap_preserve_bounded_admission(configured_job, monkeypatch):
    from ls.core.agent.runtime_lock import runtime_use
    root, job = configured_job
    monkeypatch.setattr(jobs.time, 'time', lambda: 1000)
    monkeypatch.setattr(jobs, 'resolve_catalog', lambda *_a, **_k: pytest.fail('not due or overlapping scan ran'))
    jobs.runtime._write(root/'reconciliation-run.json', {
        'schema_version': 1, 'runtime_sha256': job['runtime_sha256'],
        'started_at': 990, 'next_due': 1110, 'state': 'started'})
    assert jobs.run_due(job)['state'] == 'not_due'
    directory = root/'reconciliation-schedule'
    with runtime_use(directory, exclusive=True, timeout=0):
        assert jobs.run_due(job)['state'] == 'overlap_skipped'


def test_schedule_binding_drift_revocation_and_multiple_runtimes(configured_job, tmp_path, monkeypatch):
    root, job = configured_job
    identity = jobs.schedule_id(root)
    state = tmp_path/'scheduler'
    cron = FakeCrontab('# keep\n', present=True)
    installed = scheduler.install_schedule(identity, source_root=SOURCE_ROOT, state_root=state,
        grant_id='index-test', job={'runtime_root': str(root)}, backend=cron)
    assert installed['ok']
    monkeypatch.setattr(jobs, 'run_due', lambda _: {'ok': True, 'state': 'reconciled'})
    def run():
        return scheduler.run_bound_schedule(identity, source_root=SOURCE_ROOT,
            state_root=state, binding_sha256=installed['binding_sha256'])
    assert run()['result']['state'] == 'reconciled'
    with scheduler._file_lock(state/'schedules', identity+'-overlap.lock', blocking=False):
        assert run()['state'] == 'overlap_skipped'
    job['runtime_sha256'] = 'c'*64
    assert run()['state'] == 'job_drift'
    assert scheduler.status_schedule(identity, state_root=state, source_root=SOURCE_ROOT, backend=cron)['state'] == 'job_drift'
    with pytest.raises(scheduler.SchedulerError, match='cannot be rebound'):
        scheduler.install_schedule(identity, source_root=SOURCE_ROOT, state_root=state,
            grant_id='index-test', job={'runtime_root': str(root)}, backend=cron)
    other = 'container-'+'d'*32
    scheduler.install_schedule(other, source_root=SOURCE_ROOT, state_root=state,
        grant_id='other-test', job={'runtime_root': str(root)}, backend=cron)
    assert cron.snapshot.content.count('* * * * *') == 2
    assert scheduler.disable_schedule(identity, state_root=state, backend=cron)['state'] == 'disabled'
    assert run()['state'] == 'revoked'
    assert '# keep\n' in cron.snapshot.content and other in cron.snapshot.content
    assert identity not in cron.snapshot.content


def test_disabled_runtime_cannot_enroll_or_create_schedule(tmp_path):
    from ls.core.openpgp import container_runtime as runtime
    root = tmp_path/('e'*32)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(root.name))
    cron = FakeCrontab()
    with pytest.raises(scheduler.SchedulerError, match='unavailable or unauthorized'):
        scheduler.install_schedule(jobs.schedule_id(root), source_root=SOURCE_ROOT,
            state_root=tmp_path/'scheduler', grant_id='disabled',
            job={'runtime_root': str(root)}, backend=cron)
    assert cron.write_count == 0
    assert not (tmp_path/'scheduler').exists()
