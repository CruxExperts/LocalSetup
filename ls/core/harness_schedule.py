"""Merge one heartbeat cron block using the shared scheduler's guarded writer."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .terminal_ops import command_scheduler as cron
from .terminal_ops.command_state import atomic_json, read_json


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _merge(content: str, fragment: str, identity: str) -> tuple[str, str | None]:
    begin = f'# BEGIN LOCALSETUP_HEARTBEAT target={identity}'
    end = f'# END LOCALSETUP_HEARTBEAT target={identity}'
    block = begin+'\n'+fragment.rstrip('\n')+'\n'+end+'\n' if fragment else ''
    lines = content.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith(begin)]
    ends = [i for i, line in enumerate(lines) if line.startswith(end)]
    if not starts and not ends:
        return cron._append_block(content, block) if block else content, None
    if (len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]
            or lines[starts[0]].rstrip('\r\n') != begin or lines[ends[0]].rstrip('\r\n') != end):
        raise ValueError('Ambiguous heartbeat cron markers; preserve and inspect the current crontab')
    first, last = starts[0], ends[0]+1
    old = ''.join(lines[first:last])
    return ''.join(lines[:first])+block+''.join(lines[last:]), _sha(old)


def install_fragment(target: Path, fragment: str, *, backend=None, state_root: Path | None = None) -> dict:
    """One explicit enrollment/update; uncertain writes are observed, never replayed."""
    if '\0' in fragment or len(fragment.encode()) > 128*1024 or 'LOCALSETUP_HEARTBEAT' in fragment:
        raise ValueError('Invalid or oversized heartbeat cron fragment')
    target = target.resolve(strict=True)
    identity = _sha(str(target))
    schedule_id = 'heartbeat-'+identity[:32]
    directory = cron._schedule_state_dir(cron._state_root(state_root), create=True)
    assert directory is not None
    record_path = directory/(schedule_id+'.heartbeat.json')
    backend = backend or cron.SubprocessCrontabBackend()
    # Share the existing scheduler's crontab writer lock, bounded adapter,
    # compressed backup and lost-acknowledgement reconciliation.
    with cron._file_lock(directory, 'crontab.lock', blocking=False):
        before = cron._snapshot(backend)
        prior = read_json(record_path) if record_path.exists() else None
        if prior is not None:
            if (prior.get('schema_version') != 1 or prior.get('target') != str(target)
                    or prior.get('status') not in {'applied','not_applied','pending'}):
                raise ValueError('Invalid heartbeat cron ownership record')
            if prior['status'] == 'pending':
                if _sha(before.content) == prior.get('desired_sha256'):
                    prior['status'] = 'applied'
                    atomic_json(record_path, prior)
                elif _sha(before.content) == prior.get('previous_sha256'):
                    prior['status'] = 'not_applied'
                    atomic_json(record_path, prior)
                    raise RuntimeError('Prior heartbeat cron write was not applied; no automatic replay')
                else:
                    raise RuntimeError('Prior heartbeat cron write remains uncertain; inspect before changing it')
        desired, old_block = _merge(before.content, fragment, identity)
        if old_block is not None:
            expected = None if prior is None else prior.get(
                'block_sha256' if prior['status'] == 'applied' else 'previous_block_sha256')
            if old_block != expected:
                raise ValueError('Heartbeat cron block has no matching owner record; preserve and inspect it')
        if desired == before.content:
            return {'installed': True, 'changed': False, 'backup': None}
        if len(desired.encode()) > cron.MAX_CRONTAB_BYTES:
            raise ValueError('Merged crontab exceeds its byte limit')
        backend.validate_crontab(desired, directory)
        backup = cron._backup_crontab(directory, schedule_id, before)
        if not cron._snapshot_equal(before, cron._snapshot(backend)):
            raise RuntimeError('Crontab changed during preparation; no write dispatched')
        _, block_digest = _merge(desired, fragment, identity)
        record = dict(schema_version=1, target=str(target), status='pending',
            previous_sha256=_sha(before.content), desired_sha256=_sha(desired),
            previous_block_sha256=old_block, block_sha256=block_digest, backup=str(backup))
        atomic_json(record_path, record)
        outcome, _observed, _detail = cron._write_and_reconcile(backend, before, desired)
        record['status'] = {'applied':'applied', 'not_applied':'not_applied', 'uncertain':'pending'}[outcome]
        atomic_json(record_path, record)
        if outcome != 'applied':
            raise RuntimeError(f'Heartbeat cron update {outcome}; retained backup and operation record, no replay')
        return {'installed': True, 'changed': True, 'backup': str(backup), 'reconciled': True}
