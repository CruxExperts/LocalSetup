"""Committed repository release policy, anchor validation and sequential planning."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .git_subprocess import run_git
from . import versioning_sequence as sequence
from .versioning_constants import VERSION_SYNC_PREFIX, BREAKING_SUBJECT_RE, BREAKING_CHANGE_RE
from .versioning_models import SemVer

POLICY_PATH = '.localsetup-release.json'
POLICY = 'sequential-logical-slices'
DIGEST = re.compile(r'[0-9a-f]{40}\Z')
MAX_BYTES = 64 * 1024


def _git(root, arguments, *, check=True):
    return run_git(root, arguments, text=True, capture_output=True, check=check)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate release policy key')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Nonfinite release policy value')


def validate(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int:
        raise ValueError('Invalid release policy envelope')
    schema_version = value['schema_version']
    required = {'schema_version', 'policy', 'anchor', 'overrides'}
    if schema_version == 2:
        required |= {'major_line', 'reconciliation'}
    if (schema_version not in {1, 2} or set(value) != required
            or value['policy'] != POLICY):
        raise ValueError('Invalid release policy envelope')
    anchor = value['anchor']
    if not isinstance(anchor, dict) or set(anchor) != {'commit', 'version', 'tag'}:
        raise ValueError('Invalid release policy anchor')
    if not isinstance(anchor['commit'], str) or not DIGEST.fullmatch(anchor['commit']):
        raise ValueError('Release anchor requires a full commit SHA')
    version = anchor['version']
    if not isinstance(version, str) or len(version) > 64 or str(SemVer.parse(version)) != version:
        raise ValueError('Release anchor requires a canonical version')
    if anchor['tag'] != 'v' + version:
        raise ValueError('Release anchor tag must match its version')
    overrides = value['overrides']
    if not isinstance(overrides, list) or len(overrides) > 256:
        raise ValueError('Release policy allows at most 256 exact overrides')
    seen = set()
    for row in overrides:
        if not isinstance(row, dict) or set(row) != {'commit', 'slice', 'classification'}:
            raise ValueError('Invalid release override fields')
        if not isinstance(row['commit'], str) or not DIGEST.fullmatch(row['commit']) or row['commit'] in seen:
            raise ValueError('Release overrides require distinct full commit SHAs')
        if not isinstance(row['slice'], str) or not sequence.SLICE.fullmatch(row['slice']):
            raise ValueError('Release override requires a bounded slice identity')
        if not isinstance(row['classification'], str) or row['classification'] not in sequence.RANK:
            raise ValueError('Release override requires a release classification')
        seen.add(row['commit'])
    if schema_version == 2:
        if type(value['major_line']) is not int or value['major_line'] != 4:
            raise ValueError('The active release-line lock must select major version 4')
        reconciliation = value['reconciliation']
        expected = {'original_anchor', 'published_anchor', 'cutoff', 'reclassified_slices'}
        if not isinstance(reconciliation, dict) or set(reconciliation) != expected:
            raise ValueError('Invalid release-line reconciliation fields')
        for key in ('original_anchor', 'published_anchor'):
            row = reconciliation[key]
            if not isinstance(row, dict) or set(row) != {'commit', 'version', 'tag'}:
                raise ValueError(f'Invalid release-line {key.replace("_", " ")}')
            if not isinstance(row['commit'], str) or not DIGEST.fullmatch(row['commit']):
                raise ValueError(f'Release-line {key.replace("_", " ")} requires a full commit SHA')
            if not isinstance(row['version'], str) or str(SemVer.parse(row['version'])) != row['version']:
                raise ValueError(f'Release-line {key.replace("_", " ")} requires a canonical version')
            if row['tag'] != 'v' + row['version']:
                raise ValueError(f'Release-line {key.replace("_", " ")} tag must match its version')
        cutoff = reconciliation['cutoff']
        if not isinstance(cutoff, dict) or set(cutoff) != {'commit', 'source_version', 'corrected_version'}:
            raise ValueError('Invalid release-line cutoff')
        if not isinstance(cutoff['commit'], str) or not DIGEST.fullmatch(cutoff['commit']):
            raise ValueError('Release-line cutoff requires a full commit SHA')
        for key in ('source_version', 'corrected_version'):
            if not isinstance(cutoff[key], str) or str(SemVer.parse(cutoff[key])) != cutoff[key]:
                raise ValueError(f'Release-line cutoff {key.replace("_", " ")} must be canonical')
        if SemVer.parse(cutoff['corrected_version']).major != value['major_line']:
            raise ValueError('Corrected release-line version must match the locked major version')
        reconciled = reconciliation['reclassified_slices']
        if not isinstance(reconciled, list) or len(reconciled) != 1:
            raise ValueError('The release-line correction requires exactly one historical slice reconciliation')
        seen_slices: set[str] = set()
        seen_commits: set[str] = set()
        for row in reconciled:
            if not isinstance(row, dict) or set(row) != {'slice', 'classification', 'commits'}:
                raise ValueError('Invalid historical slice reconciliation fields')
            if not isinstance(row['slice'], str) or not sequence.SLICE.fullmatch(row['slice']) or row['slice'] in seen_slices:
                raise ValueError('Historical slice reconciliation requires a distinct lowercase slice identity')
            if row['classification'] != 'minor':
                raise ValueError('The one-time release-line reconciliation can map a historical major only to minor')
            commits = row['commits']
            if (not isinstance(commits, list) or not 2 <= len(commits) <= 32
                    or any(not isinstance(sha, str) or not DIGEST.fullmatch(sha) or sha in seen_commits for sha in commits)):
                raise ValueError('Historical slice reconciliation requires distinct full commit SHAs')
            seen_slices.add(row['slice'])
            seen_commits.update(commits)
    return value


def load(root: Path, head: str) -> dict | None:
    """Read only the selected commit's bounded regular blob, never loose policy."""
    entry = _git(root, ['ls-tree', '-z', head, '--', POLICY_PATH]).stdout
    if not entry:
        return None
    fields, path = entry.rstrip('\0').split('\t', 1)
    mode, kind, digest = fields.split()
    if mode not in {'100644', '100755'} or kind != 'blob' or path != POLICY_PATH:
        raise ValueError('Release policy must be a regular committed Git blob')
    size = int(_git(root, ['cat-file', '-s', digest]).stdout)
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Release policy exceeds the 64 KiB limit')
    raw = run_git(root, ['cat-file', 'blob', digest], text=False, capture_output=True, check=True).stdout
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Invalid committed release policy JSON') from exc
    return validate(value)


def validate_anchor(root: Path, configuration: dict, head: str) -> None:
    anchor = configuration['anchor']
    sha = anchor['commit']
    kind = _git(root, ['cat-file', '-t', sha], check=False)
    if kind.returncode or kind.stdout.strip() != 'commit':
        raise ValueError('Release anchor commit is unavailable')
    if _git(root, ['merge-base', '--is-ancestor', sha, head], check=False).returncode:
        raise ValueError('Release anchor must be an ancestor of the planned head')
    if str(sequence.committed_version(root, sha)) != anchor['version']:
        raise ValueError('Release anchor committed VERSION differs from policy')


def _tag_commit(root: Path, tag: str, label: str) -> str:
    result = _git(root, ['rev-parse', '--verify', f'refs/tags/{tag}^{{commit}}'], check=False)
    if result.returncode or result.stdout.strip() == '':
        raise ValueError(f'Release-line {label} tag is unavailable')
    return result.stdout.strip()


def _is_ancestor(root: Path, ancestor: str, descendant: str, message: str) -> None:
    if _git(root, ['merge-base', '--is-ancestor', ancestor, descendant], check=False).returncode:
        raise ValueError(message)


def validate_reconciliation(root: Path, configuration: dict, head: str) -> dict:
    """Verify the one-time 4.x arithmetic bridge against immutable Git history."""
    from . import versioning as api

    if configuration['schema_version'] != 2:
        return {}
    reconciliation = configuration['reconciliation']
    original = reconciliation['original_anchor']
    published = reconciliation['published_anchor']
    cutoff = reconciliation['cutoff']
    anchor = configuration['anchor']

    for row, label in ((original, 'original'), (published, 'pre-correction published')):
        if _tag_commit(root, row['tag'], label) != row['commit']:
            raise ValueError(f'Release-line {label} tag does not resolve to its recorded commit')
        if str(sequence.committed_version(root, row['commit'])) != row['version']:
            raise ValueError(f'Release-line {label} commit VERSION differs from its record')
    if _tag_commit(root, anchor['tag'], 'current published anchor') != anchor['commit']:
        raise ValueError('Current published release anchor tag does not resolve to its recorded commit')
    _is_ancestor(root, original['commit'], published['commit'], 'Original 4.x anchor must precede the published history anchor')
    _is_ancestor(root, published['commit'], cutoff['commit'], 'Published history anchor must precede the release-line cutoff')
    _is_ancestor(root, cutoff['commit'], head, 'Release-line cutoff must be an ancestor of the planned head')
    if str(sequence.committed_version(root, cutoff['commit'])) != cutoff['source_version']:
        raise ValueError('Release-line cutoff committed VERSION differs from its recorded source version')

    # Before the corrected release is published, the v5 anchor remains current.
    # Afterwards, the ordinary anchor advances to a published 4.x commit.
    if anchor['commit'] == published['commit']:
        arithmetic_start = cutoff['commit']
        arithmetic_version = SemVer.parse(cutoff['corrected_version'])
    else:
        if SemVer.parse(anchor['version']).major != configuration['major_line']:
            raise ValueError('Published anchor is neither the preserved pre-correction release nor a release on the locked 4.x line')
        _is_ancestor(root, cutoff['commit'], anchor['commit'], 'Corrected 4.x release anchor must follow the release-line cutoff')
        anchor_version = SemVer.parse(anchor['version'])
        corrected_version = SemVer.parse(cutoff['corrected_version'])
        if (anchor_version.major, anchor_version.minor, anchor_version.patch) < (
                corrected_version.major, corrected_version.minor, corrected_version.patch):
            raise ValueError('Corrected 4.x release anchor cannot precede the reconciled release version')
        arithmetic_start = anchor['commit']
        arithmetic_version = SemVer.parse(anchor['version'])

    legacy = plan_sequential(root, base=original['commit'], head=cutoff['commit'])
    if not legacy['ok'] or legacy['target_version'] != cutoff['source_version']:
        raise ValueError('Historical release syncs do not validate to the recorded pre-correction cutoff version')

    commits = api.list_integrated_commits(root, original['commit'], cutoff['commit'])
    exclusions = {commit.sha: sequence.exclusion(root, commit) for commit in commits}
    excluded = {sha for sha, reason in exclusions.items() if reason}
    net_commits, _ = sequence.cancel_reverts(commits, excluded, repo_root=root)
    classifications = {
        commit.sha: 'none' if commit.sha in excluded else api._source_classification(commit)
        for commit in commits
    }
    by_sha = {commit.sha: commit for commit in commits}
    for row in reconciliation['reclassified_slices']:
        all_major_members = set()
        for commit in commits:
            _, slice_id = sequence.metadata(commit.body)
            if slice_id == row['slice'] and commit.sha not in excluded and classifications[commit.sha] == 'major':
                all_major_members.add(commit.sha)
        expected_members = set(row['commits'])
        if all_major_members != expected_members:
            raise ValueError('Historical slice reconciliation must name every and only major member of that exact slice')
        for sha in expected_members:
            commit = by_sha.get(sha)
            if (commit is None or sha in excluded
                    or not (BREAKING_SUBJECT_RE.match(commit.subject) or BREAKING_CHANGE_RE.search(commit.body))
                    or api.release_type_override(commit.body) != 'major'):
                raise ValueError(f'Historical reconciliation member {sha} is not an explicit breaking major source')
            _, slice_id = sequence.metadata(commit.body)
            if slice_id != row['slice']:
                raise ValueError(f'Historical reconciliation member {sha} does not belong to its recorded slice')
            classifications[sha] = row['classification']
    corrected, corrected_slices = sequence.fold(
        net_commits, classifications, SemVer.parse(original['version'])
    )
    if str(corrected) != cutoff['corrected_version']:
        raise ValueError('Historical slices do not reconcile to the recorded corrected 4.x version')

    return {
        'original_target_version': legacy['target_version'],
        'historical_sync_checks': legacy['version_sync_checks'],
        'corrected_target_version': str(corrected),
        'corrected_logical_slices': corrected_slices,
        'arithmetic_start': arithmetic_start,
        'arithmetic_version': str(arithmetic_version),
    }


def validated_overrides(configuration, commits, exclusions):
    overrides = {} if configuration is None else {row['commit']: row for row in configuration['overrides']}
    known = {commit.sha: commit for commit in commits}
    for sha, row in overrides.items():
        if sha not in known or sha in exclusions or sequence.REVERT.search(known[sha].body):
            raise ValueError(f'Release override {sha} must name an unpublished source commit')
        commit = known[sha]
        if (BREAKING_SUBJECT_RE.match(commit.subject) or BREAKING_CHANGE_RE.search(commit.body)) and row['classification'] != 'major':
            raise ValueError(f'Release override {sha} cannot downgrade a breaking change')
    return overrides


def plan(root: Path, *, base=None, head=None, ref=None, policy=None) -> dict:
    from . import versioning as api
    selected_head = api.resolve_head(root, head)
    configuration = load(root, selected_head)
    selected_policy = configuration['policy'] if configuration is not None else policy or 'patch-default'
    if configuration is not None and policy is not None and policy != selected_policy:
        raise ValueError('Explicit release policy conflicts with the committed repository contract')
    if selected_policy == 'patch-default':
        return api._plan_version_legacy(root, base=base, head=selected_head, ref=ref)
    if selected_policy != POLICY:
        raise ValueError(f'Unknown release policy: {selected_policy}')
    line_state = None
    if configuration is not None:
        validate_anchor(root, configuration, selected_head)
        if configuration['schema_version'] == 2:
            line_state = validate_reconciliation(root, configuration, selected_head)
    return plan_sequential(root, base=base, head=selected_head, ref=ref,
                           configuration=configuration, line_state=line_state)


def guard_target(root: Path, target: str) -> None:
    """Protect explicit mutation targets under a committed repository contract."""
    from . import versioning as api
    selected_head = api._rev_parse(root, "HEAD")
    if selected_head is None or load(root, selected_head) is None:
        return
    result = plan(root)
    if not result['repairable']:
        raise ValueError('Release history requires reconciliation before version mutation')
    if target != result['target_version']:
        raise ValueError('Requested version differs from the committed release policy target')


def plan_sequential(repo_root: Path, *, base: str | None = None, head: str | None = None,
                    ref: str | None = None, configuration: dict | None = None,
                    line_state: dict | None = None) -> dict:
    from . import versioning as api
    resolved_head = api.resolve_head(repo_root, head)
    base_payload = api.resolve_base_with_metadata(repo_root, base, resolved_head)
    comparison_base = str(base_payload["base"])
    reconciliation = (configuration or {}).get('reconciliation')
    if reconciliation:
        if line_state is None:
            line_state = validate_reconciliation(repo_root, configuration, resolved_head)
        resolved_base = line_state['arithmetic_start']
    else:
        resolved_base = configuration['anchor']['commit'] if configuration is not None else comparison_base
    base_resolution = base_payload['base_resolution']
    if api._run_git(repo_root, ["merge-base", "--is-ancestor", resolved_base, resolved_head], check=False).returncode:
        raise ValueError("Release base must be an ancestor of the selected head")
    commits = api.list_integrated_commits(repo_root, resolved_base, resolved_head)
    exclusions = {commit.sha: reason for commit in commits if (reason := sequence.exclusion(repo_root, commit))}
    known = {commit.sha for commit in commits}
    for commit in commits:
        for reverted in sequence.REVERT.findall(commit.body):
            if reverted not in known and (api._rev_parse(repo_root, reverted) is None or
                    api._run_git(repo_root, ["merge-base", "--is-ancestor", reverted, resolved_base], check=False).returncode):
                raise ValueError(f"Revert {commit.sha} does not name a source in this range or published base ancestry")
    overrides = validated_overrides(configuration, commits, exclusions)
    net_commits, canceled = sequence.cancel_reverts(commits, set(exclusions), repo_root=repo_root, overrides=overrides)
    release_type_required = [{"sha": item.sha, "subject": item.subject,
                              "message": "Breaking release markers require an explicit Release-Type: major compatibility decision."}
                             for item in net_commits if item.sha not in exclusions and sequence.requires_major_decision(item) and overrides.get(item.sha, {}).get('classification') != 'major']
    classifications = {commit.sha: "none" if commit.sha in exclusions else api._source_classification(commit) for commit in commits}
    classifications.update({sha: row['classification'] for sha, row in overrides.items()})
    bump = api.max_bump(classifications[commit.sha] for commit in net_commits)
    base_version = (SemVer.parse(line_state['arithmetic_version']) if reconciliation
                    else sequence.committed_version(repo_root, resolved_base))
    target, logical_slices = sequence.fold(net_commits, classifications, base_version, overrides=overrides)
    current = sequence.committed_version(repo_root, resolved_head)
    worktree = api.read_version(repo_root)
    sync_commits = [commit for commit in commits if commit.subject.startswith(VERSION_SYNC_PREFIX)]
    version_sync_present = bool(sync_commits)
    sync_checks = []
    for commit in commits:
        if commit not in sync_commits:
            continue
        ancestors = [item for item in api.list_integrated_commits(repo_root, resolved_base, commit.sha) if item.sha != commit.sha]
        prefix, _ = sequence.cancel_reverts(ancestors, set(exclusions), repo_root=repo_root, overrides=overrides)
        prefix_types = {item.sha: classifications[item.sha] for item in prefix}
        prefix_target, _ = sequence.fold(prefix, prefix_types, base_version, overrides=overrides)
        sync_checks.append({"sha": commit.sha, "expected_version": str(prefix_target),
                            "recorded_version": str(api.version_from_sync_commit(commit.subject)),
                            "committed_version": str(sequence.committed_version(repo_root, commit.sha)),
                            "ok": (api.version_from_sync_commit(commit.subject) == prefix_target
                                   and sequence.committed_version(repo_root, commit.sha) == prefix_target)})
    version_sync_matches_target = all(check["ok"] for check in sync_checks)
    latest_sync_matches_target = not sync_commits or api.version_from_sync_commit(sync_commits[-1].subject) == target
    head_version_matches_target = current == target
    # A schema-2 reconciliation changes the canonical arithmetic baseline even
    # when the cutoff contains no new source bump. Require the committed
    # VERSION to catch up before treating that reconciled history as ready.
    head_version_required = bool(reconciliation) or version_sync_present or bump != "none"
    major_line = None if configuration is None else configuration.get('major_line')
    major_line_violations = [
        {"sha": commit.sha, "subject": commit.subject,
         "classification": classifications[commit.sha],
         "message": f"Major version increases are disabled while the {major_line}.x release-line lock is active."}
        for commit in net_commits
        if major_line is not None and classifications[commit.sha] == 'major'
    ]
    ok = (
        (not head_version_required or head_version_matches_target)
        and version_sync_matches_target
        and latest_sync_matches_target
        and not release_type_required
        and not major_line_violations
    )
    return {
        "ok": ok,
        "policy": POLICY,
        "repairable": version_sync_matches_target and not release_type_required and not major_line_violations,
        "comparison_base": comparison_base,
        "comparison_base_resolution": base_resolution,
        "anchor": None if configuration is None else configuration['anchor'],
        "published_anchor": (configuration['anchor'] if line_state is not None else None),
        "major_line": major_line,
        "line_reconciliation": line_state,
        "release_overrides": list(overrides.values()),
        "logical_slices": logical_slices,
        "excluded_commits": [{"sha": sha, "reason": reason} for sha, reason in exclusions.items()],
        "version_sync_checks": sync_checks,
        "latest_sync_matches_target": latest_sync_matches_target,
        "ref": ref,
        "base": resolved_base,
        "head": resolved_head,
        "base_resolution": (base_resolution if configuration is None else api._base_result(
            status="resolved",
            strategy="committed_line_cutoff" if reconciliation and resolved_base != configuration['anchor']['commit'] else "committed_release_anchor",
            ref=(reconciliation['cutoff']['commit'] if reconciliation and resolved_base != configuration['anchor']['commit']
                 else configuration["anchor"]["tag"]),
            sha=resolved_base, attempts=[])),
        "base_version": str(base_version),
        "current_version": str(current),
        "worktree_version": str(worktree),
        "target_version": str(target),
        "major_minor": target.major_minor,
        "bump": bump,
        "release_type_required": bool(release_type_required),
        "release_type_required_commits": release_type_required,
        "major_line_violations": major_line_violations,
        "version_sync_present": version_sync_present,
        "version_sync_matches_target": version_sync_matches_target,
        "commit_count": len(commits),
        "net_commit_count": len(net_commits),
        "canceled_reverts": canceled,
        "commits": [
            {
                "sha": commit.sha,
                "subject": commit.subject,
                "bump": classifications[commit.sha],
                "raw_bump": api.classify_commit(commit.subject, commit.body),
                "release_type_required": sequence.requires_major_decision(commit) and overrides.get(commit.sha, {}).get('classification') != 'major',
                "files": api.changed_files(repo_root, commit.sha),
            }
            for commit in net_commits
        ],
    }
