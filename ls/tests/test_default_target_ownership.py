import json
from pathlib import Path

import pytest

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


@pytest.mark.parametrize('scope', ['repo', 'personal', 'both'])
def test_default_target_update_retains_recorded_ownership(tmp_path, monkeypatch, capsys, scope):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home'
    monkeypatch.setattr(cli, '_is_global_shim_invocation', lambda: False)
    apply_plan(root, build_install_plan(root, home, skills=['ls-localsetup-project-context'], platform_ids=['cursor'],
        skill_scope=scope, attach_mode='portable'), home)
    receipt = root / '.localsetup/lock.json'
    old = json.loads(receipt.read_text());registry = Path(old['registry_path'])
    before = receipt.read_bytes(), registry.read_bytes()
    prefix = ['--source-root', str(root), '--home', str(home)]
    assert cli.main(prefix + ['plan']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['auto_mode'] == ('inferred_existing' if scope == 'repo' else f'recorded_{scope}')
    assert before == (receipt.read_bytes(), registry.read_bytes())
    assert cli.main(prefix + ['update']) == 0
    assert json.loads(capsys.readouterr().out)['auto_mode'] == report['auto_mode']
    new = json.loads(receipt.read_text())
    assert new['skill_scope'] == scope
    assert new['platforms'] == old['platforms'] == ['cursor']
    assert new['adapter_targets'] == old['adapter_targets']
    assert new['attach_mode'] == old['attach_mode'] == 'portable'
    for current, previous in zip(new['personal_adapter_targets'], old['personal_adapter_targets']):
        assert all(current[k] == v for k, v in previous.items())
    assert len(new['personal_adapter_targets']) == len(old['personal_adapter_targets'])
    assert json.loads(registry.read_text()).get('personal_owners') == json.loads(before[1]).get('personal_owners')
