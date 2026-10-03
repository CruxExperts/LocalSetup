import json
from pathlib import Path

from ls.core import cli
from ls.core.apply import apply_plan
from ls.core.plan import build_install_plan
from ls.tests.test_install_flow import make_temp_repo


def test_cli_automatic_personal_plan_update_and_repair_first(tmp_path, capsys):
    root = make_temp_repo(tmp_path);home = tmp_path / 'home';target = tmp_path / 'target'
    target.mkdir()
    apply_plan(root, build_install_plan(root, home, skills=['ls-context'], platform_ids=['cursor'],
                                        target_root=target, skill_scope='personal'), home)
    receipt = target / '.localsetup/lock.json'
    registry = Path(json.loads(receipt.read_text())['registry_path'])
    owners = json.loads(registry.read_text())['personal_owners']
    before = (receipt.read_bytes(), registry.read_bytes())
    def invoke(command):
        result = cli._main(['--source-root', str(root), '--home', str(home), command,
                            '--target-directory', str(target)])
        payload = json.loads(capsys.readouterr().out)
        assert result == 0, payload
        return payload
    planned = invoke('plan')
    assert planned['auto_mode'] == 'recorded_personal'
    assert planned['rollback']['skill_scope'] == 'personal'
    assert before == (receipt.read_bytes(), registry.read_bytes())
    source = root / 'ls/skills/ls-context/SKILL.md'
    source.write_text(source.read_text() + '\nCLI fixture update.\n')
    updated = invoke('update')
    assert updated['ok'] and updated['auto_mode'] == 'recorded_personal'
    assert json.loads(registry.read_text())['personal_owners'] == owners
    assert 'CLI fixture update.' in (home / '.agents/skills/ls-context/SKILL.md').read_text()
    (home / '.agents/skills/ls-context').unlink()
    repair = invoke('plan')
    assert repair['auto_mode'] == 'repair_required'
    assert any(a['kind'] == 'attach_personal_path' for a in repair['actions'])
    assert not (home / '.agents/skills/ls-context').exists()


def test_cli_preserves_custom_codex_agent_for_one_recorded_update(tmp_path, capsys):
    root = make_temp_repo(tmp_path)
    home = tmp_path / 'home'
    plan = build_install_plan(root, home=home, packs=['core'], platform_ids=['codex'])
    apply_plan(root, plan, home=home)
    agent_path = home / '.codex/agents/guardian_subagent.toml'
    custom_bytes = b'name = "guardian_subagent"\nmodel = "custom"\n# retained bytes: \xff\n'
    agent_path.write_bytes(custom_bytes)
    skill_source = root / 'ls/skills/ls-context/SKILL.md'
    skill_source.write_bytes(skill_source.read_bytes() + b'\nRecorded CLI update fixture.\n')
    global_root = next(action.path for action in plan.actions if action.kind == 'install_skills')
    installed_skill = global_root / 'ls-context' / 'SKILL.md'
    before_update = installed_skill.read_bytes()

    def invoke(command, *extra):
        result = cli._main([
            '--source-root', str(root), '--home', str(home), command,
            '--codex-agent-conflict', 'preserve', *extra,
        ])
        payload = json.loads(capsys.readouterr().out)
        assert result == 0, payload
        return payload

    preview = invoke('plan')
    preserve_row = {
        'name': 'guardian_subagent',
        'path': str(agent_path),
        'reason': 'existing readable Codex agent file differs from selected source',
    }
    assert preview['preflight']['ok']
    assert preview['preflight']['preserved_codex_agents'] == [preserve_row]
    assert preview['rollback']['codex_agents'] == ['guardian_subagent']
    assert installed_skill.read_bytes() == before_update

    updated = invoke('update')
    assert updated['ok']
    assert 'Recorded CLI update fixture.' in installed_skill.read_text(encoding='utf-8')
    assert agent_path.read_bytes() == custom_bytes
    assert updated['preflight']['preserved_codex_agents'] == [preserve_row]
    assert updated['preserved_codex_agents'] == [preserve_row]
    assert updated['installed_codex_agents'] == []
    assert not any(item.startswith('install_codex_agents:') for item in updated['executed'])
    journal = json.loads(Path(updated['journal']).read_text(encoding='utf-8'))
    assert all(item.get('path') != str(agent_path) for item in journal['touched'])

    lock_path = root / '.localsetup/lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    assert lock['codex_agents'] == ['guardian_subagent']
    assert lock['installed_codex_agents'] == []
    assert lock['preserved_codex_agents'] == [preserve_row]
    lock_after_preserving_update = lock_path.read_bytes()
    installed_after_preserving_update = installed_skill.read_bytes()

    code = cli.main([
        '--source-root', str(root), '--home', str(home), 'update',
    ])
    error = capsys.readouterr().err
    assert code == 2
    assert 'codex_agent_conflict' in error
    assert agent_path.read_bytes() == custom_bytes
    assert installed_skill.read_bytes() == installed_after_preserving_update
    assert lock_path.read_bytes() == lock_after_preserving_update
