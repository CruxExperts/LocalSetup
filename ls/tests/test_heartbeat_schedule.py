"""Explicit scheduled review selection and loss-aware current-user cron updates."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile

import pytest
import yaml

from ls.core import harness, harness_schedule
from ls.core.terminal_ops.command_scheduler import CrontabSnapshot, CronWriteResult
from ls.tests.codex_heartbeat_test_helpers import ROOT, load_runtime, state_root, write_config


class Crontab:
    def __init__(self, content='MAILTO=operator\n0 1 * * * keep-existing\n'):
        self.content, self.writes = content, 0
        self.lose_ack = self.lose_read = False

    def read_crontab(self):
        if self.lose_read and self.writes:
            raise OSError('read unavailable')
        return CrontabSnapshot(True, self.content)

    def validate_crontab(self, content, directory):
        assert content and directory.is_dir()

    def write_crontab(self, content):
        self.writes += 1
        self.content = content
        if self.lose_ack:
            raise TimeoutError('ack lost')
        return CronWriteResult(0)


def test_managed_cron_preserves_other_jobs_and_reconciles_lost_ack(tmp_path):
    target=tmp_path/'target';target.mkdir()
    state=tmp_path/'state';backend=Crontab();original=backend.content
    backend.lose_ack=True
    result=harness_schedule.install_fragment(target,'*/15 * * * * review\n',backend=backend,state_root=state)
    assert result['installed'] and backend.content.startswith(original) and backend.writes==1
    with tarfile.open(result['backup']) as archive:
        assert archive.extractfile('crontab.txt').read().decode()==original
    assert not harness_schedule.install_fragment(target,'*/15 * * * * review\n',backend=backend,state_root=state)['changed']
    assert backend.writes==1
    harness_schedule.install_fragment(target,'',backend=backend,state_root=state)
    assert backend.content==original and backend.writes==2


def test_pending_cron_is_reconciled_without_replay_and_foreign_edits_refused(tmp_path):
    target=tmp_path/'target';target.mkdir()
    state=tmp_path/'state';backend=Crontab();backend.lose_read=True
    with pytest.raises(RuntimeError,match='uncertain'):
        harness_schedule.install_fragment(target,'*/15 * * * * review\n',backend=backend,state_root=state)
    assert backend.writes==1
    backend.lose_read=False
    assert harness_schedule.install_fragment(target,'*/15 * * * * review\n',backend=backend,state_root=state)['installed']
    assert backend.writes==1
    backend.content=backend.content.replace('review','modified')
    with pytest.raises(ValueError,match='owner record'):
        harness_schedule.install_fragment(target,'',backend=backend,state_root=state)
    assert backend.writes==1


@pytest.mark.parametrize('damage',['orphan','duplicate','incomplete','changed-before-write'])
def test_cron_conflicts_never_dispatch(tmp_path,damage):
    target=tmp_path/'target';target.mkdir();state=tmp_path/'state';backend=Crontab()
    block,_=harness_schedule._merge('', '* * * * * fixture\n', harness_schedule._sha(str(target)))
    if damage=='orphan':backend.content+=block
    elif damage=='duplicate':backend.content+=block+block
    elif damage=='incomplete':backend.content+=block.splitlines(keepends=True)[0]
    else:
        def mutate(content,directory):backend.content+='0 2 * * * concurrent\n'
        backend.validate_crontab=mutate
    with pytest.raises((ValueError,RuntimeError)):
        harness_schedule.install_fragment(target,'*/15 * * * * review\n',backend=backend,state_root=state)
    assert backend.writes==0


def test_generated_trigger_runs_selected_review_and_keeps_mode_across_enable(tmp_path):
    target=tmp_path/'target';target.mkdir()
    agent={'enabled':True,'profile':'review'}
    profiles={'review':{'client':'fixture','launcher':'direct-argv','prompt_transport':'none',
        'command':[sys.executable,'-c',"from pathlib import Path; Path('reviewed').write_text('done')"]}}
    write_config(target,agent=agent,agent_profiles=profiles)
    harness.enable(ROOT,target,scheduled_agent=True)
    harness.enable(ROOT,target)
    assert harness.plan(ROOT,target)['command_plan']['commands'][0]['id']=='review-agent'
    manifest=target/harness.CRON_MANIFEST
    data=yaml.safe_load(manifest.read_text())
    data['triggers']['unrelated']={'schedule':'0 1 * * *'}
    manifest.write_text(yaml.safe_dump(data))
    rendered=subprocess.run([sys.executable,str(harness._cron_ctl(ROOT)),'--manifest',str(manifest),
        'install','--repo-root',str(target),'--trigger',harness.HEARTBEAT_TRIGGER_ID],
        capture_output=True,text=True,check=True)
    lines=[line for line in rendered.stdout.splitlines() if line and not line.startswith('#')]
    assert len(lines)==1 and lines[0].startswith('*/15 ')
    command=shlex.split(lines[0].split(maxsplit=5)[5])
    assert command[:3]==[sys.executable,'-I','-B']
    result=subprocess.run(command,cwd=target,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr+result.stdout
    assert (target/'reviewed').read_text()=='done'
    (target/'reviewed').unlink()
    harness.enable(ROOT,target,scheduled_agent=False)
    assert harness.plan(ROOT,target)['command_plan']['commands']==[]
    # An older cron command also observes the new persisted selection.
    assert subprocess.run(command,cwd=target,capture_output=True,timeout=30).returncode==0
    assert not (target/'reviewed').exists()
    harness.disable(ROOT,target)
    assert harness.run(ROOT,target,scheduled=True)['status']=='skipped'


@pytest.mark.parametrize('failure',['missing','execution'])
def test_collection_survives_agent_outage_on_each_run(tmp_path,failure):
    runtime=load_runtime()
    collect=[sys.executable,'-c',"from pathlib import Path; p=Path('inventory'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    profiles={} if failure=='missing' else {'review':{'client':'fixture','launcher':'direct-argv',
        'prompt_transport':'none','command':[sys.executable,'-c','raise SystemExit(1)']}}
    write_config(tmp_path,hooks={'before':[{'command':collect}],'after':[]},
        agent={'enabled':True,'profile':'review'},agent_profiles=profiles)
    for _ in range(2):
        if failure=='missing':
            with pytest.raises(runtime.HeartbeatError,match='profile not found'):runtime.run_once(target_root=tmp_path)
        else:assert runtime.run_once(target_root=tmp_path)['status']=='failed'
        latest=json.loads((state_root(tmp_path)/'latest.json').read_text())
        assert latest['status']=='failed'
        runtime.validate_staged_run(state_root(tmp_path)/latest['path'])
    assert (tmp_path/'inventory').read_text()=='xx'


def test_schedule_selection_requires_enabled_profile_and_cannot_override_disable(tmp_path):
    path=write_config(tmp_path,enabled=False)
    original=path.read_bytes()
    with pytest.raises(ValueError,match='Configure and enable'):
        harness.enable(ROOT,tmp_path,scheduled_agent=True)
    assert path.read_bytes()==original and not (tmp_path/harness.CRON_MANIFEST).exists()
    assert harness.run(ROOT,tmp_path,scheduled=True)['reason']=='heartbeat.disabled'
    with pytest.raises(load_runtime().HeartbeatError,match='cannot override'):
        harness.run(ROOT,tmp_path,scheduled=True,force=True)
    config=yaml.safe_load(path.read_text());config['heartbeat']['scheduled_agent']='yes'
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(load_runtime().HeartbeatError,match='must be a boolean'):
        harness.run(ROOT,tmp_path,scheduled=True)


def test_live_installer_owns_only_heartbeat_trigger_and_preserves_other_entries(tmp_path,monkeypatch):
    run=subprocess.run
    def isolated_control(argv, **kwargs):
        if str(harness._cron_ctl(ROOT)) in argv:
            assert argv[:3]==[sys.executable,'-I','-B']
        return run(argv, **kwargs)
    monkeypatch.setattr(subprocess,'run',isolated_control)
    target=tmp_path/'target';target.mkdir();state=tmp_path/'state';backend=Crontab()
    original=backend.content
    manifest=target/harness.CRON_MANIFEST;manifest.parent.mkdir()
    manifest.write_text(yaml.safe_dump({'triggers':{'other':{'schedule':'0 1 * * *'}},'tasks':[]}))
    install=harness_schedule.install_fragment
    monkeypatch.setattr(harness_schedule,'install_fragment',
        lambda target,fragment:install(target,fragment,backend=backend,state_root=state))
    harness.enable(ROOT,target,install_crontab=True,yes=True)
    assert backend.content.startswith(original)
    assert '# Trigger: codex-heartbeat' in backend.content and '# Trigger: other' not in backend.content
    assert backend.writes==1
    harness.disable(ROOT,target,install_crontab=True,yes=True)
    assert backend.content==original and backend.writes==2
