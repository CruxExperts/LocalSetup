"""Logical release policy against small real histories, without generators."""
import subprocess

import pytest

from ls.core.versioning import plan_version


def git(root, *arguments):
    return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.com')
    (tmp_path / 'VERSION').write_text('4.4.0\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'base')
    git(tmp_path, 'tag', 'base')
    return tmp_path


def commit(repo, subject, body='', path='change'):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(target.read_text() + 'change\n' if target.exists() else 'change\n')
    git(repo, 'add', path)
    git(repo, 'commit', '-qm', subject, '-m', body)
    return git(repo, 'rev-parse', 'HEAD')


def plan(repo):
    return plan_version(repo, base='base', policy='sequential-logical-slices')


def test_sequential_unique_slices_and_group_upgrade_at_first_anchor(repo):
    first = commit(repo, 'fix: foundation', 'Release-Slice: capability')
    middle = commit(repo, 'fix: independent')
    last = commit(repo, 'feat: finish', 'Release-Slice: capability')
    result = plan(repo)
    assert result['target_version'] == '4.5.1'
    assert [(row['anchor'], row['classification']) for row in result['logical_slices']] == [(first, 'minor'), (middle, 'patch')]
    assert result['logical_slices'][0]['source_shas'] == [first, last]


def test_distinct_features_each_increment_and_reset_patch(repo):
    commit(repo, 'feat: one')
    commit(repo, 'fix: polish')
    commit(repo, 'feat: two')
    assert plan(repo)['target_version'] == '4.6.0'


@pytest.mark.parametrize('body', ['Release-Type: invalid', 'Release-Type: minor\nRelease-Type: minor',
                                'Release-Slice: Bad ID', 'Release-Slice: a\nRelease-Slice: b'])
def test_invalid_metadata_refuses_plan(repo, body):
    commit(repo, 'fix: change', body)
    with pytest.raises(ValueError, match='Release|release'):
        plan(repo)


def test_breaking_requires_major_decision(repo):
    commit(repo, 'feat!: break')
    result = plan(repo)
    assert result['release_type_required'] and result['target_version'] == '5.0.0'
    commit(repo, 'fix!: hidden break', 'Release-Type: patch')
    with pytest.raises(ValueError, match='Release-Type: major'):
        plan(repo)


def test_generated_paths_not_receipt_subject_control_exclusion(repo):
    receipt = commit(repo, 'docs: regenerated', path='ls/docs/_generated/example.json')
    authored = commit(repo, 'docs: refresh receipt', path='ls/docs/guide.md')
    result = plan(repo)
    assert result['target_version'] == '4.4.1'
    assert result['excluded_commits'] == [{'sha': receipt, 'reason': 'generated_receipt'}]
    assert result['logical_slices'][0]['anchor'] == authored


def test_generated_public_count_refresh_is_not_a_release_slice(repo):
    readme = repo / "README.md"
    features = repo / "ls" / "docs" / "FEATURES.md"
    readme.write_text(
        "# README\n\n"
        "<!-- facts-block:start -->\n"
        "- Workflow packages: `19`\n"
        "<!-- facts-block:end -->\n\n"
        "## The short version\n\n"
        "LocalSetup packages:\n\n"
        "- 106 shipped capability skills plus 19 first-class workflow packages for debugging.\n\n"
        "## How it fits together\n",
        encoding="utf-8",
    )
    features.parent.mkdir(parents=True, exist_ok=True)
    features.write_text(
        "# Features\n\n"
        "<!-- facts-block:start -->\n"
        "- Workflow packages: `19`\n"
        "<!-- facts-block:end -->\n\n"
        "## Skills And Interoperability\n\n"
        "| Capability | What it gives you |\n"
        "|---|---|\n"
        "| 106 shipped skills plus 19 workflow packages | Practical capabilities and orchestration flows |\n\n"
        "## Workflow Control\n",
        encoding="utf-8",
    )
    git(repo, 'add', 'README.md', 'ls/docs/FEATURES.md')
    git(repo, 'commit', '-qm', 'docs: add public count receipts')

    readme.write_text(
        readme.read_text(encoding="utf-8")
        .replace("`19`", "`20`")
        .replace("plus 19 first-class", "plus 20 first-class"),
        encoding="utf-8",
    )
    features.write_text(
        features.read_text(encoding="utf-8")
        .replace("`19`", "`20`")
        .replace("plus 19 workflow", "plus 20 workflow"),
        encoding="utf-8",
    )
    git(repo, 'add', 'README.md', 'ls/docs/FEATURES.md')
    git(repo, 'commit', '-qm', 'docs: refresh generated public counts')
    refresh = git(repo, 'rev-parse', 'HEAD')

    result = plan(repo)

    assert result['target_version'] == '4.4.1'
    assert result['excluded_commits'] == [{'sha': refresh, 'reason': 'generated_receipt'}]


def test_manual_public_count_edit_is_not_excluded_as_generated_receipt(repo):
    readme = repo / "README.md"
    initial = (
        "# README\n\n"
        "<!-- facts-block:start -->\n"
        "- Workflow packages: `19`\n"
        "<!-- facts-block:end -->\n\n"
        "## Historical notes\n\n"
        "A previous edition listed 106 shipped capability skills plus 19 first-class workflow packages.\n"
    )
    readme.write_text(initial, encoding="utf-8")
    git(repo, 'add', 'README.md')
    git(repo, 'commit', '-qm', 'docs: record historical count example')

    readme.write_text(
        initial.replace("plus 19 first-class workflow packages", "plus 20 first-class workflow packages"),
        encoding="utf-8",
    )
    git(repo, 'add', 'README.md')
    git(repo, 'commit', '-qm', 'docs: refresh historical count example')
    refresh = git(repo, 'rev-parse', 'HEAD')

    result = plan(repo)

    assert result['target_version'] == '4.4.2'
    assert result['excluded_commits'] == []


def test_real_merge_orders_first_parent_before_integrated_side_branch(repo):
    branch = git(repo, 'branch', '--show-current')
    git(repo, 'checkout', '-qb', 'side')
    side = commit(repo, 'fix: side', path='side')
    git(repo, 'checkout', '-q', branch)
    main = commit(repo, 'feat: main', path='main')
    git(repo, 'merge', '--no-ff', '-qm', 'integration without Merge prefix', 'side')
    result = plan(repo)
    assert [row['anchor'] for row in result['logical_slices']] == [main, side]
    assert result['target_version'] == '4.5.1'
    assert result['excluded_commits'][0]['reason'] == 'merge'


def test_nonancestor_explicit_base_refused(repo):
    commit(repo, 'fix: one')
    git(repo, 'branch', 'other')
    git(repo, 'reset', '--hard', 'base')
    commit(repo, 'fix: divergent')
    with pytest.raises(ValueError, match='ancestor'):
        plan_version(repo, base='other', policy='sequential-logical-slices')


def test_exact_native_revert_and_partial_group_refusal(repo):
    commit(repo, 'feat: removed')
    git(repo, 'revert', '--no-edit', 'HEAD')
    assert plan(repo)['target_version'] == '4.4.0'
    commit(repo, 'fix: first', 'Release-Slice: group')
    commit(repo, 'fix: second', 'Release-Slice: group')
    git(repo, 'revert', '--no-edit', 'HEAD')
    with pytest.raises(ValueError, match='Partially reverted'):
        plan(repo)


def test_fake_subject_revert_is_not_silent_cancellation(repo):
    commit(repo, 'Revert "missing"')
    with pytest.raises(ValueError, match='exact native Git'):
        plan(repo)


def test_published_revert_is_new_patch(repo):
    commit(repo, 'fix: published')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'revert', '--no-edit', 'HEAD')
    assert plan_version(repo, base=base, policy='sequential-logical-slices')['target_version'] == '4.4.1'


def sync(repo, value):
    (repo / 'VERSION').write_text(value+'\n')
    git(repo, 'add', 'VERSION')
    git(repo, 'commit', '-qm', 'chore: sync release version '+value)


def test_intermediate_sync_validates_its_prefix_not_final_target(repo):
    commit(repo, 'fix: first')
    sync(repo, '4.4.1')
    commit(repo, 'feat: second')
    sync(repo, '4.5.0')
    result = plan(repo)
    assert result['ok']
    assert [row['expected_version'] for row in result['version_sync_checks']] == ['4.4.1', '4.5.0']


def test_invalid_historical_sync_is_reported_not_grandfathered(repo):
    commit(repo, 'fix: first')
    sync(repo, '4.4.8')
    commit(repo, 'feat: second')
    sync(repo, '4.5.0')
    result = plan(repo)
    assert not result['ok']
    assert not result['version_sync_checks'][0]['ok']


def test_sync_subject_cannot_hide_arbitrary_source(repo):
    commit(repo, 'chore: sync release version 4.4.0', path='code.py')
    with pytest.raises(ValueError, match='non-version paths'):
        plan(repo)


def test_sync_cannot_hide_authored_doc_changes(repo):
    commit(repo, 'docs: guide', path='ls/docs/guide.md')
    commit(repo, 'chore: sync release version 4.4.1', path='ls/docs/guide.md')
    with pytest.raises(ValueError, match='authored content'):
        plan(repo)


def test_fixture_copy_excludes_private_root_but_preserves_upstream_agents(tmp_path, monkeypatch):
    from ls.tests import versioning_test_helpers as helpers
    source = tmp_path / 'source'
    for name in ('.localsetup-release.json', '.agents/state/private', '.localsetup-maint/private', '.localsetup-maint/boundary.example.yaml', 'vendor/sdk/.agents/skills/SKILL.md'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('retained fixture')
    monkeypatch.setattr(helpers, '__file__', str(source / 'ls/tests/versioning_test_helpers.py'))
    destination = tmp_path / 'destination'
    destination.mkdir()
    copied = helpers.copy_full_repo(destination)
    assert not (copied / '.agents').exists()
    assert not (copied / '.localsetup-release.json').exists()
    assert sorted(path.name for path in (copied / '.localsetup-maint').iterdir()) == ['boundary.example.yaml']
    assert (copied / 'vendor/sdk/.agents/skills/SKILL.md').read_text() == 'retained fixture'


def test_policy_selection_preserves_generic_patch_default(repo):
    commit(repo, 'feat: capability')
    commit(repo, 'fix: maintenance')
    assert plan_version(repo, base='base')['target_version'] == '4.4.1'
    assert plan(repo)['target_version'] == '4.5.1'
    with pytest.raises(ValueError, match='Unknown release policy'):
        plan_version(repo, base='base', policy='unknown')


def test_unknown_revert_target_is_not_assumed_published(repo):
    commit(repo, 'Revert "unknown"', 'This reverts commit '+'0'*40+'.')
    with pytest.raises(ValueError, match='published base ancestry'):
        plan(repo)


def test_sync_subject_and_committed_version_must_both_match(repo):
    commit(repo, 'fix: first')
    (repo / 'VERSION').write_text('4.4.8\n')
    git(repo, 'add', 'VERSION')
    git(repo, 'commit', '-qm', 'chore: sync release version 4.4.1')
    sync(repo, '4.4.1')
    result = plan(repo)
    assert not result['ok']
    assert result['version_sync_checks'][0]['committed_version'] == '4.4.8'


@pytest.mark.parametrize('missing', ['base', 'head'])
def test_sequential_requires_committed_version_without_worktree_fallback(repo, missing):
    git(repo, 'rm', 'VERSION')
    git(repo, 'commit', '-qm', 'fix: missing version')
    if missing == 'base':
        git(repo, 'tag', '-f', 'base')
        (repo / 'VERSION').write_text('4.4.0\n')
        git(repo, 'add', 'VERSION')
        git(repo, 'commit', '-qm', 'fix: restore version')
    else:
        (repo / 'VERSION').write_text('4.4.0\n')
    with pytest.raises(ValueError, match='no committed VERSION'):
        plan(repo)


def test_mixed_revert_cannot_hide_additional_source(repo):
    original = commit(repo, 'feat: original', path='feature')
    git(repo, 'revert', '--no-commit', original)
    (repo / 'additional').write_text('additional source\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Revert "feat: original"', '-m', f'This reverts commit {original}.')
    with pytest.raises(ValueError, match='not an exact inverse'):
        plan(repo)
    assert (repo / 'additional').read_text() == 'additional source\n'


def test_valid_but_inaccurate_revert_sha_does_not_cancel_feature(repo):
    original = commit(repo, 'feat: original', path='feature')
    commit(repo, 'Revert "feat: original"', f'This reverts commit {original}.', path='unrelated')
    with pytest.raises(ValueError, match='not an exact inverse'):
        plan(repo)
    assert (repo / 'feature').exists()


def test_exact_inverse_preserves_later_unrelated_source(repo):
    original = commit(repo, 'feat: original', path='feature')
    retained = commit(repo, 'fix: independent', path='unrelated')
    git(repo, 'revert', '--no-edit', original)
    result = plan(repo)
    assert result['target_version'] == '4.4.1'
    assert [row['anchor'] for row in result['logical_slices']] == [retained]
    assert (repo / 'unrelated').read_text() == 'change\n'


def test_indented_explicit_major_uses_same_parser_for_decision_flags(repo):
    commit(repo, 'feat!: accepted break', '  Release-Type: major')
    result = plan(repo)
    assert result['target_version'] == '5.0.0'
    assert result['release_type_required'] is False
    assert result['commits'][0]['release_type_required'] is False


def test_inverse_path_comparison_preserves_raw_filename_bytes(repo):
    original = commit(repo, 'feat: original', path='file\r\n')
    commit(repo, 'fix: distinct path', path='file\n')
    git(repo, 'rm', 'file\n')
    git(repo, 'commit', '-qm', 'Revert "feat: original"', '-m', f'This reverts commit {original}.')
    with pytest.raises(ValueError, match='not an exact inverse'):
        plan(repo)
    assert (repo / 'file\r\n').exists()
