"""Explicit tool-free reviews preserve collection, bounded receipts and privacy."""
import json
import os
from pathlib import Path
import sys

import pytest
import yaml

from ls.core.agent.completion_contract import envelope
from ls.tests.codex_heartbeat_test_helpers import ROOT, load_runtime, state_root, write_config
from ls.tests.test_agent_provider import config

runtime = load_runtime()
import heartbeat_review as review


@pytest.fixture
def configured(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / 'target'; target.mkdir()
    source = tmp_path / 'profiles.json'
    source.write_text(json.dumps({'schema_version': 1, 'profiles': {'test': config()}})); source.chmod(0o600)
    grant = tmp_path / 'grant.json'
    grant.write_text(json.dumps(dict(schema_version=1, read=['health.txt'], write=[], disclose=['health.txt'], recipes={})))
    grant.chmod(0o600)
    template = ROOT / 'ls/skills/ls-codex-heartbeat-checks/templates/codex_heartbeat.yaml'
    profile = yaml.safe_load(template.read_text())['agent_profiles']['lscli-review']
    profile.update(profiles=str(source), profile='test', grant=str(grant), context=['health.txt'])
    agent = dict(enabled=True, profile='review')
    return target, profile, agent


def test_plan_never_reads_context_or_resolves_credentials(configured, monkeypatch):
    from ls.core.agent import profile_credentials
    target, profile, agent = configured
    monkeypatch.setattr(review, '_resolve', lambda *_: ['/protected/python'])
    monkeypatch.setattr(profile_credentials, 'resolve', lambda *_: pytest.fail('plan must not resolve'))
    plan = review.plan(profile, agent, 'review', target)
    assert not (target / 'health.txt').exists()
    assert '--context=health.txt' in plan['command'] and 'complete' in plan['command']
    assert json.loads(plan['stdin_text'])['max_attempts'] == 1
    assert plan['protocol_options']['mode'] == 'completion'
    assert profile['prompt'] not in repr(plan['command'])


@pytest.mark.parametrize('case', ['valid', 'truncated', 'extra', 'schema', 'model', 'failure', 'exit'])
def test_review_receipt_checks_one_matching_bounded_outcome(case):
    value = envelope('succeeded', model='expected', data={'summary': 'Useful review', 'findings': []}, attempts=1)
    if case == 'schema': value['data']['findings'] = ['x'] * 17
    if case == 'model': value['model'] = 'other'
    if case == 'failure': value = envelope('unavailable')
    raw = (json.dumps(value) + ('\n{}\n' if case == 'extra' else '\n')).encode()
    receipt = review.Receipt('expected')
    if case == 'truncated': raw = raw[:-1]
    if case in ('schema', 'model', 'extra', 'truncated'):
        with pytest.raises(ValueError):
            receipt.feed(raw); receipt.finish(0)
    else:
        receipt.feed(raw[:10]); receipt.feed(raw[10:])
        result = receipt.finish(1 if case == 'exit' else 0)
        assert result['completed'] is (case == 'valid')
        assert ('review' in result) is (case == 'valid')


@pytest.mark.parametrize('failure', [False, True])
def test_collected_context_reaches_review_and_records_remain_private(configured, monkeypatch, failure):
    target, profile, agent = configured
    # Real subprocess and real broker, synthetic completion only: no external provider.
    code = f"""import json,sys,threading,time
from pathlib import Path
sys.path.insert(0,{str(ROOT)!r})
from ls.core.agent.completion_context import attach
from ls.core.agent.completion_contract import envelope
args=sys.argv[1:]
def arg(name):return args[args.index(name)+1]
raw=attach(sys.stdin.read(),['health.txt'],Path(arg('--grant')),Path(arg('--workspace')),time.monotonic()+5,threading.Event())
assert json.loads(raw)['input']['context'][0]['text']=='fresh health observation'
print(json.dumps(envelope('succeeded',model={config()['model']!r},data={{'summary':'Review complete','findings':['Check updates']}},attempts=1)))
"""
    if failure: code = 'raise SystemExit(3)'
    monkeypatch.setattr(review, '_resolve', lambda *_: [sys.executable, '-c', code])
    collect = [sys.executable, '-c', "from pathlib import Path; Path('health.txt').write_text('fresh health observation')"]
    write_config(target, agent=agent, agent_profiles={'review': profile},
                 hooks={'before': [{'command': collect}], 'after': []})
    prior = os.umask(0o022)
    try: result = runtime.run_once(target_root=target)
    finally: os.umask(prior)
    assert result['ok'] is (not failure)
    assert (target / 'health.txt').read_text() == 'fresh health observation'
    latest = json.loads((state_root(target) / 'latest.json').read_text())
    run = state_root(target) / latest['path']
    log = json.loads((run / 'command-log.json').read_text())
    entry = log['commands'][-1]
    assert entry['stdout_tail'] == entry['stderr_tail'] == ''
    assert 'fresh health observation' not in json.dumps(entry.get('protocol'))
    if not failure: assert entry['protocol']['review']['summary'] == 'Review complete'
    for path in run.glob('*.json'):
        assert path.stat().st_mode & 0o077 == 0
