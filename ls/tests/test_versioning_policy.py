"""Committed policy activation and mutation boundaries on real Git histories."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import ls.core.versioning as versioning_module
from ls.core.versioning import plan_version, publish_preflight, sync_version_files
from ls.core.versioning_policy import POLICY_PATH, guard_target, validate
from ls.tests.test_versioning_sequence import repo, git, commit, sync


def configuration(repo, overrides=()):
    return {'schema_version': 1, 'policy': 'sequential-logical-slices',
            'anchor': {'commit': git(repo, 'rev-parse', 'base'), 'version': '4.4.0', 'tag': 'v4.4.0'},
            'overrides': list(overrides)}


def activate(repo, value=None, raw=None):
    (repo / POLICY_PATH).write_text(raw if raw is not None else json.dumps(value or configuration(repo)))
    git(repo, 'add', POLICY_PATH)
    git(repo, 'commit', '-qm', 'chore: select release policy', '-m', 'Release-Type: none')


def test_committed_policy_anchor_independent_of_comparison_base(repo):
    first = commit(repo, 'feat: first')
    commit(repo, 'feat: second')
    activate(repo)
    for base in ('base', first, 'HEAD'):
        result = plan_version(repo, base=base)
        assert result['target_version'] == '4.6.0'
        assert result['base'] == git(repo, 'rev-parse', 'base')
        assert result['comparison_base'] == git(repo, 'rev-parse', base)
        assert result['base_resolution']['sha'] == result['base']
        assert result['base_resolution']['strategy'] == 'committed_release_anchor'
        assert result['repairable'] and not result['ok']
    assert plan_version(repo)['target_version'] == '4.6.0'
    with pytest.raises(ValueError, match='conflicts'):
        plan_version(repo, policy='patch-default')


def test_loose_policy_cannot_activate_disable_or_replace_committed_contract(repo):
    first = commit(repo, 'feat: first')
    (repo / POLICY_PATH).write_text('invalid loose JSON')
    assert plan_version(repo, base='base')['policy'] == 'patch-default'
    activate(repo)
    (repo / POLICY_PATH).unlink()
    assert plan_version(repo, base='base')['target_version'] == '4.5.0'
    (repo / POLICY_PATH).write_text('invalid replacement')
    assert plan_version(repo, base='base')['target_version'] == '4.5.0'
    assert plan_version(repo, base='base', head=first)['policy'] == 'patch-default'


def test_exact_overrides_preserve_raw_history_and_prefix_convergence(repo):
    first = commit(repo, 'feat: original doctor')
    sync(repo, '4.4.1')
    later = commit(repo, 'feat: finish doctor')
    rows = [{'commit': sha, 'slice': 'doctor', 'classification': 'patch'} for sha in (first, later)]
    activate(repo, configuration(repo, rows))
    result = plan_version(repo)
    assert result['ok'] and result['target_version'] == '4.4.1'
    assert result['version_sync_checks'][0]['ok']
    doctor = result['logical_slices'][0]
    assert doctor['anchor'] == first and doctor['source_shas'] == [first, later]
    assert result['release_overrides'] == rows
    row = next(row for row in result['commits'] if row['sha'] == first)
    assert row['raw_bump'] == 'minor' and row['bump'] == 'patch'
    assert git(repo, 'show', '-s', '--format=%B', first) == 'feat: original doctor'


@pytest.mark.parametrize('raw', ['{}', '{"schema_version":1,"schema_version":1}', '[]', 'NaN', 'x' * 65537])
def test_invalid_committed_policy_fails_before_mutation(repo, raw):
    activate(repo, raw=raw)
    before = git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()
    with pytest.raises(ValueError):
        sync_version_files(repo, '9.0.0')
    assert (git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()) == before


@pytest.mark.parametrize('mutation', ['unknown', 'boolean', 'short-sha', 'tag', 'version', 'duplicate'])
def test_strict_policy_schema(mutation, repo):
    value = configuration(repo)
    if mutation == 'unknown': value['extra'] = True
    if mutation == 'boolean': value['schema_version'] = True
    if mutation == 'short-sha': value['anchor']['commit'] = '1234567'
    if mutation == 'tag': value['anchor']['tag'] = 'v4.5.0'
    if mutation == 'version': value['anchor']['version'] = '04.4.0'
    if mutation == 'duplicate':
        row = {'commit': value['anchor']['commit'], 'slice': 'x', 'classification': 'patch'}
        value['overrides'] = [row, row]
    with pytest.raises(ValueError): validate(value)


def test_symlink_policy_is_not_followed(repo):
    (repo / POLICY_PATH).symlink_to('VERSION')
    git(repo, 'add', POLICY_PATH)
    git(repo, 'commit', '-qm', 'chore: link')
    with pytest.raises(ValueError, match='regular committed'):
        plan_version(repo)


@pytest.mark.parametrize('target', ['published', 'unknown', 'receipt', 'revert', 'breaking'])
def test_override_membership_and_breaking_boundary(repo, target):
    if target == 'published': sha = git(repo, 'rev-parse', 'base')
    elif target == 'unknown': sha = 'a' * 40
    elif target == 'receipt': sha = commit(repo, 'docs: receipt', path='ls/docs/_generated/test.json')
    elif target == 'breaking': sha = commit(repo, 'feat!: breaking')
    else:
        original = commit(repo, 'fix: reversible')
        git(repo, 'revert', '--no-edit', original)
        sha = git(repo, 'rev-parse', 'HEAD')
    activate(repo, configuration(repo, [{'commit': sha, 'slice': 'x', 'classification': 'patch'}]))
    with pytest.raises(ValueError, match='unpublished source|downgrade'):
        plan_version(repo)


def test_anchor_version_must_match_committed_tree(repo):
    value = configuration(repo)
    value['anchor'].update(version='4.3.0', tag='v4.3.0')
    activate(repo, value)
    with pytest.raises(ValueError, match='committed VERSION'):
        plan_version(repo)


def test_nonrepairable_history_stops_preflight_and_explicit_target(repo):
    commit(repo, 'fix: first')
    sync(repo, '4.4.7')
    activate(repo)
    assert not plan_version(repo)['repairable']
    before = git(repo, 'rev-parse', 'HEAD'), (repo / 'VERSION').read_bytes()
    assert publish_preflight(repo, fix=True)['reason'] == 'invalid_release_history'
    with pytest.raises(ValueError, match='reconciliation'):
        sync_version_files(repo, '4.4.1')
    assert (git(repo, 'rev-parse', 'HEAD'), (repo / 'VERSION').read_bytes()) == before
    assert not git(repo, 'status', '--porcelain')


def test_explicit_target_cannot_bypass_canonical_arithmetic(repo):
    commit(repo, 'feat: capability')
    activate(repo)
    with pytest.raises(ValueError, match='differs'):
        guard_target(repo, '4.4.1')
    guard_target(repo, '4.5.0')


def line_locked_history(repo):
    original = git(repo, 'rev-parse', 'base')
    git(repo, 'tag', 'v4.4.0', original)
    first = commit(repo, 'feat!: page contract',
                   'Release-Type: major\nRelease-Slice: issue-100-compat\n\nBREAKING CHANGE: readers now return pages.')
    second = commit(repo, 'feat!: signed envelope',
                    'Release-Type: major\nRelease-Slice: issue-100-compat\n\nBREAKING CHANGE: agents require signed envelopes.')
    sync(repo, '5.0.0')
    published = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'tag', 'v5.0.0', published)
    commit(repo, 'feat: follow-up feature')
    sync(repo, '5.1.0')
    cutoff = git(repo, 'rev-parse', 'HEAD')
    value = {
        'schema_version': 2,
        'policy': 'sequential-logical-slices',
        'anchor': {'commit': published, 'version': '5.0.0', 'tag': 'v5.0.0'},
        'overrides': [],
        'major_line': 4,
        'reconciliation': {
            'original_anchor': {'commit': original, 'version': '4.4.0', 'tag': 'v4.4.0'},
            'published_anchor': {'commit': published, 'version': '5.0.0', 'tag': 'v5.0.0'},
            'cutoff': {'commit': cutoff, 'source_version': '5.1.0', 'corrected_version': '4.6.0'},
            'reclassified_slices': [{
                'slice': 'issue-100-compat', 'classification': 'minor', 'commits': [first, second],
            }],
        },
    }
    activate(repo, value)
    return value, first, second, cutoff


def test_line_reconciliation_validates_old_syncs_and_starts_four_x_arithmetic(repo):
    _value, _first, _second, cutoff = line_locked_history(repo)
    result = plan_version(repo)
    assert not result['ok']
    assert result['repairable']
    assert result['current_version'] == '5.1.0'
    assert result['target_version'] == '4.6.0'
    assert result['base_version'] == '4.6.0'
    assert result['base'] == cutoff
    assert result['published_anchor']['version'] == '5.0.0'
    assert result['line_reconciliation']['original_target_version'] == '5.1.0'
    assert result['line_reconciliation']['corrected_target_version'] == '4.6.0'
    assert all(item['ok'] for item in result['line_reconciliation']['historical_sync_checks'])
    assert result['version_sync_checks'] == []


def test_reconciled_preflight_syncs_corrected_version_before_receipt(repo, monkeypatch):
    line_locked_history(repo)

    def prepare_candidate(root, target):
        (root / 'VERSION').write_text(f'{target}\n')
        return {'version': target, 'changed_candidates': ['VERSION']}

    monkeypatch.setattr(versioning_module, 'prepare_version_sync_candidate', prepare_candidate)
    prepared = publish_preflight(repo, fix=False)
    assert prepared['reason'] == 'prepared_not_ready'
    assert prepared['plan']['bump'] == 'none'
    assert prepared['prepared_paths'] == ['VERSION']
    assert git(repo, 'diff', '--cached', '--name-only') == ''
    assert git(repo, 'diff', '--name-only') == 'VERSION'
    git(repo, 'checkout', '--', 'VERSION')

    def sync_version(root, target):
        (root / 'VERSION').write_text(f'{target}\n')
        return {'version': target, 'changed_candidates': ['VERSION']}

    monkeypatch.setattr(versioning_module, 'sync_version_files', sync_version)
    monkeypatch.setattr(versioning_module, 'stage_version_files', lambda root: git(root, 'add', 'VERSION'))
    monkeypatch.setattr(versioning_module, 'check_version_files', lambda root, target: {'ok': True})
    fixed = publish_preflight(repo, fix=True)
    assert fixed['ok'] and fixed['plan']['ok']
    assert fixed['plan']['bump'] == 'none'
    assert [row['type'] for row in fixed['commits']] == ['version_sync']
    sync_commit = fixed['commits'][0]['sha']
    assert git(repo, 'show', '-s', '--format=%s', sync_commit) == 'chore: sync release version 4.6.0'
    assert git(repo, 'diff-tree', '--no-commit-id', '--name-only', '-r', sync_commit) == 'VERSION'
    assert git(repo, 'status', '--porcelain') == ''


def test_line_lock_allows_patch_and_minor_but_rejects_future_major(repo):
    line_locked_history(repo)
    sync(repo, '4.6.0')
    commit(repo, 'fix: correction')
    pending = plan_version(repo)
    assert pending['target_version'] == '4.6.1'
    assert pending['repairable'] and pending['bump'] == 'patch'
    sync(repo, '4.6.1')
    assert plan_version(repo)['ok']
    commit(repo, 'feat!: incompatible change',
           'Release-Type: major\nRelease-Slice: future-break\n\nBREAKING CHANGE: contract changes.')
    blocked = plan_version(repo)
    assert not blocked['ok'] and not blocked['repairable']
    assert blocked['major_line'] == 4
    assert blocked['major_line_violations'][0]['classification'] == 'major'
    before = git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()
    with pytest.raises(ValueError, match='reconciliation'):
        sync_version_files(repo, '5.0.0')
    assert (git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()) == before


def test_reconciled_policy_advances_to_published_four_x_anchor(repo):
    value, _first, _second, _cutoff = line_locked_history(repo)
    sync(repo, '4.6.0')
    release_commit = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'tag', 'v4.6.0', release_commit)
    value['anchor'] = {'commit': release_commit, 'version': '4.6.0', 'tag': 'v4.6.0'}
    activate(repo, value)
    result = plan_version(repo)
    assert result['ok'] and result['base'] == release_commit
    assert result['base_version'] == '4.6.0'
    assert result['published_anchor']['tag'] == 'v4.6.0'


def test_schema_two_requires_four_x_reconciliation_and_preserves_schema_one(repo):
    legacy = configuration(repo)
    assert validate(legacy)['schema_version'] == 1
    value = {
        'schema_version': 2,
        'policy': 'sequential-logical-slices',
        'anchor': legacy['anchor'],
        'overrides': [],
        'major_line': 4,
        'reconciliation': {
            'original_anchor': legacy['anchor'], 'published_anchor': legacy['anchor'],
            'cutoff': {'commit': 'a' * 40, 'source_version': '5.1.0', 'corrected_version': '4.6.0'},
            'reclassified_slices': [{'slice': 'issue-100-compat', 'classification': 'minor',
                                     'commits': ['b' * 40, 'c' * 40]}],
        },
    }
    assert validate(value)['major_line'] == 4
    for mutation in ('wrong_line', 'wrong_corrected_major', 'missing_member', 'major_mapping'):
        changed = json.loads(json.dumps(value))
        if mutation == 'wrong_line': changed['major_line'] = 5
        elif mutation == 'wrong_corrected_major': changed['reconciliation']['cutoff']['corrected_version'] = '5.6.0'
        elif mutation == 'missing_member': changed['reconciliation']['reclassified_slices'][0]['commits'] = ['b' * 40]
        else: changed['reconciliation']['reclassified_slices'][0]['classification'] = 'major'
        with pytest.raises(ValueError):
            validate(changed)


def test_public_cli_uses_committed_anchor_and_refuses_explicit_target(repo):
    commit(repo, 'feat: capability')
    activate(repo)
    entry = Path(__file__).resolve().parents[1] / 'tools/localsetup.py'
    command = [sys.executable, str(entry), '--source-root', str(repo)]
    result = subprocess.run([*command, 'version-plan', '--base', 'HEAD'], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)['target_version'] == '4.5.0'
    result = subprocess.run([*command, 'version-sync', '--target', '9.0.0'], capture_output=True, text=True)
    assert result.returncode != 0
    assert (repo / 'VERSION').read_text() == '4.4.0\n'
    assert not git(repo, 'status', '--porcelain')


@pytest.mark.parametrize('consumer', ['release-push', 'pre-push'])
@pytest.mark.parametrize('invalid', ['prefix', 'json'])
def test_consumers_reject_invalid_history_without_mutation(repo, consumer, invalid):
    commit(repo, 'fix: first')
    sync(repo, '4.4.7')
    activate(repo, raw='{}' if invalid == 'json' else None)
    source = Path(__file__).resolve().parents[2]
    (repo / 'ls').symlink_to(source / 'ls', target_is_directory=True)
    before = git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()
    if consumer == 'release-push':
        command = [sys.executable, str(source / 'ls/tools/localsetup.py'), '--source-root', str(repo), consumer]
        payload = ''
    else:
        command = ['bash', str(source / '.githooks/pre-push')]
        payload = f'refs/heads/main {before[0]} refs/heads/main {git(repo, "rev-parse", "base")}\n'
    result = subprocess.run(command, cwd=repo, input=payload, capture_output=True, text=True)
    assert result.returncode != 0
    assert (git(repo, 'rev-parse', 'HEAD'), git(repo, 'status', '--porcelain'), (repo / 'VERSION').read_bytes()) == before


def test_schema_and_repository_archive_boundary(repo):
    import io
    import tarfile
    import jsonschema
    source = Path(__file__).resolve().parents[2]
    schema = json.loads((source / 'ls/config/release-policy.schema.json').read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(configuration(repo), schema)
    jsonschema.validate(json.loads((source / POLICY_PATH).read_text()), schema)
    activate(repo)
    (repo / '.gitattributes').write_text((source / '.gitattributes').read_text())
    git(repo, 'add', '.gitattributes')
    git(repo, 'commit', '-qm', 'chore: archive boundary')
    archive = subprocess.check_output(['git', 'archive', 'HEAD'], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        assert POLICY_PATH not in bundle.getnames()
        assert 'VERSION' in bundle.getnames()


def test_nonancestor_anchor_and_merge_override_rejected(repo):
    branch = git(repo, 'branch', '--show-current')
    git(repo, 'checkout', '-qb', 'side')
    side = commit(repo, 'fix: side', path='side')
    git(repo, 'checkout', '-q', branch)
    value = configuration(repo)
    value['anchor']['commit'] = side
    activate(repo, value)
    with pytest.raises(ValueError, match='ancestor'):
        plan_version(repo)
    git(repo, 'merge', '--no-ff', '-qm', 'merge side', 'side')
    merge = git(repo, 'rev-parse', 'HEAD')
    activate(repo, configuration(repo, [{'commit': merge, 'slice': 'x', 'classification': 'minor'}]))
    with pytest.raises(ValueError, match='unpublished source'):
        plan_version(repo)
