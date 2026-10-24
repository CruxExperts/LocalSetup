"""Snapshot coverage, late arrivals and publication rollback with real keys."""
from dataclasses import replace
import copy
from pathlib import Path
import shutil

import pytest

from ls.core.openpgp import artifacts
from ls.core.openpgp.artifact_records import parse_provenance, schema_envelope, validate_logical
from ls.core.openpgp.container_consolidation import prepare_snapshot
from ls.core.openpgp.container_delta_codec import create_patch
from ls.core.openpgp.container_delta_records import FileDelta
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReader
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_schema import UnsupportedRequiredFeature, validate_protected_schema
from ls.core.openpgp.container_snapshot_records import CoveredFile, SnapshotClaim, snapshot_claim
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_versions import VersionNode, build_version_graph, version_heads
from ls.tests.test_agent_container_reads import prepare
from ls.tests.test_container_delta_codec import codec, evidence, private
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def test_snapshot_schema_and_authority_boundary(tmp_path):
    covered = CoveredFile('1'*32, '2'*64, '3'*32, 4, '5'*64)
    claim = SnapshotClaim(covered.version_id, covered.commitment_sha256, (covered,))
    assert snapshot_claim(claim.record()) == claim
    assert len(claim.checkpoint_sha256) == 64
    with pytest.raises(ValueError):
        replace(claim, covered=(covered, covered))
    with pytest.raises(ValueError):
        replace(claim, source_commitment='6'*64)
    with pytest.raises(UnsupportedRequiredFeature):
        validate_protected_schema(schema_envelope(snapshot=True),
                                  supported_required_features=frozenset(schema_envelope()['required_features']))
    with pytest.raises(artifacts.ArtifactError):
        prepare_snapshot(tmp_path/'absent', reader=None, authorized=False,
            output_dir=tmp_path, namespace=None, publication_policy=None,
            trust_state_path=None, gnupg_home=None, passphrase_reference=None, secret_resolver=None)
    assert not list(tmp_path.iterdir())


def test_complete_snapshot_preserves_exact_coverage_and_late_branch(tmp_path, codec, separate_machine_env):
    env = separate_machine_env
    before, other = b'first file\n'*3000, b'untouched second file'
    after = before[:300]+b'changed'+before[310:]
    base = prepare(env, tmp_path, (before, other))[0]
    read_args = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    publish_args = dict(policy=env.policies['origin'], trust_state_path=env.paths['origin'],
        gnupg_home=env.homes['origin'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    namespace = ContainerNamespace('tenant', 'project', 'a'*32)
    def inspect(volume):
        with LocalFileRangeSource(volume.path) as source:
            return artifacts.inspect_container_metadata(source, **read_args)
    def approve(prepared):
        assert len(prepared.volumes) == 1
        with LocalFileRangeSource(prepared.volumes[0].path) as source:
            return artifacts.approve_container_distribution(source, output_dir=prepared.volumes[0].path.parent,
                policy=env.policies['controller'], trust_state_path=env.paths['controller'],
                gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    base_metadata = inspect(base)
    first, second = base_metadata.logical['files']
    patch = tmp_path/'patch'
    patch_claim = create_patch(private(tmp_path/'old', before), private(tmp_path/'new', after), patch,
        base=evidence(before), result=evidence(after), policy=codec, budget=_Budget(ContainerPreparationLimits()))
    assert patch_claim is not None
    delta = FileDelta(base_metadata.logical['version_id'], base_metadata.logical_commitment_sha256,
        first['file_id'], evidence(before), patch_claim, evidence(after), '7'*32)
    prepared = artifacts.prepare_container((
        ContainerInput(patch, first['file_id'], parse_provenance(first['provenance']), delta),
        ContainerInput(private(tmp_path/'second', other), second['file_id'], parse_provenance(second['provenance']))),
        output_dir=tmp_path/'carriers', namespace=namespace, **publish_args,
        artifact_id=base_metadata.logical['artifact_id'], parent_versions=(delta.base_version,))
    selected = approve(prepared)
    selected_metadata = inspect(selected)
    late = approve(artifacts.prepare_container((ContainerInput(private(tmp_path/'late', b'other branch'), first['file_id']),),
        output_dir=tmp_path/'carriers', namespace=namespace, **publish_args,
        artifact_id=prepared.artifact_id, parent_versions=(delta.base_version,)))
    late_metadata = inspect(late)
    incoming = tmp_path/'incoming'; incoming.mkdir(mode=0o700)
    shutil.copyfile(base.path, incoming/'base.lssc')
    shutil.copyfile(selected.path, incoming/'selected.lssc')
    scans = []
    def inventory(check):
        check(); scans.append(True)
        return tuple(sorted(incoming.glob('*.lssc')))
    limits = ContainerPreparationLimits(timeout_seconds=240)
    reader = ContainerReader(**read_args, source_resolver=inventory, delta_codec=codec, limits=limits)
    output = tmp_path/'snapshot'; output.mkdir(mode=0o700)
    arrivals = []
    def arrival():
        if not arrivals and list(output.glob('.lssc-prep-*')):
            shutil.copyfile(late.path, incoming/'late.lssc')
            arrivals.append(True)
    args = dict(reader=reader, authorized=True, namespace=namespace,
        publication_policy=publish_args['policy'], **{k:v for k,v in publish_args.items() if k!='policy'})
    budget = _Budget(limits)
    result = prepare_snapshot(incoming/'selected.lssc', output_dir=output, publication_check=arrival,
        expected_commitment=prepared.logical_commitment_sha256, _budget=budget, **args)
    assert len(scans) == 1 and arrivals == [True] and budget.temporary == 0
    assert result.claim.source_version == prepared.version_id
    assert {(item.version_id, item.file_id) for item in result.claim.covered} == {
        (delta.base_version, first['file_id']), (prepared.version_id, first['file_id']),
        (prepared.version_id, second['file_id'])}
    assert [item.change_id for item in result.claim.covered if item.change_id] == [delta.change_id]
    snapshot = approve(result.prepared)
    metadata = inspect(snapshot)
    assert snapshot_claim(metadata.logical['snapshot']) == result.claim
    assert len(metadata.logical['files']) == 2
    assert all('delta' not in item for item in metadata.logical['files'])
    for item in metadata.logical['files']:
        original = next(file for file in selected_metadata.logical['files'] if file['file_id']==item['file_id'])
        assert item == {key:value for key,value in original.items() if key!='delta'}
        with LocalFileRangeSource(snapshot.path) as source:
            extract_container_file({snapshot.key: source}, item['file_id'], item['file_id'], authorized=True, **read_args)
        assert (Path(reader.policy.boundary)/item['file_id']).read_bytes() == (after if item['file_id']==first['file_id'] else other)
    altered = copy.deepcopy(metadata.logical)
    altered['files'].pop()
    with pytest.raises(ValueError):
        validate_logical(altered)
    nodes = [VersionNode(item.logical['artifact_id'], item.logical['version_id'],
        tuple(item.logical['parent_versions']), item.logical_commitment_sha256)
        for item in (base_metadata, selected_metadata, late_metadata, metadata)]
    assert version_heads(build_version_graph(nodes), artifact_id=prepared.artifact_id) == tuple(sorted((
        late_metadata.logical['version_id'], result.prepared.version_id)))
    assert (incoming/'late.lssc').read_bytes() == late.path.read_bytes()

    failed = tmp_path/'failed'; failed.mkdir(mode=0o700)
    marker = failed/'keep'; marker.write_text('unrelated')
    checks = []
    def revoked_after_publish():
        if list(failed.glob('*.lssc')):
            checks.append(True)
            raise PermissionError('publication authority revoked')
    budget = _Budget(limits)
    with pytest.raises(artifacts.ArtifactError):
        prepare_snapshot(incoming/'selected.lssc', output_dir=failed,
            publication_check=revoked_after_publish, _budget=budget, **args)
    assert checks and list(failed.iterdir()) == [marker] and budget.temporary == 0
    assert not list(Path(reader.policy.boundary).glob('.lssc-snapshot-*'))
    assert not list(output.glob('.lssc-prep-*'))
