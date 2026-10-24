"""Complete snapshot approval keeps current authority and exact covered bytes."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from ls.core.openpgp import artifacts
from ls.core.openpgp.container_consolidation import PreparedSnapshot, approve_snapshot
from ls.core.openpgp.container_catalog import CatalogContext
from ls.core.openpgp.container_packing import PackingLimits
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReadError
from ls.core.openpgp.container_publication_job import publish_prepared_job
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_snapshot_records import CoveredFile, SnapshotClaim
from ls.core.openpgp.container_sources import BytesRangeSource, LocalFileRangeSource
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, SCOPE, separate_machine_env
from ls.tests.test_content_addressed_publication import Carrier


def test_snapshot_approval_requires_separate_authority_before_work(tmp_path):
    with pytest.raises(artifacts.ArtifactError):
        approve_snapshot(None, authorized=False, output_dir=tmp_path, policy=None,
            trust_state_path=None, gnupg_home=None, passphrase_reference=None, secret_resolver=None)
    assert not list(tmp_path.iterdir())


def test_real_snapshot_set_approval_binds_complete_inventory_and_rolls_back(tmp_path, separate_machine_env):
    env = separate_machine_env
    data = b'complete source file\n' * 1800
    source = tmp_path/'input'
    source.write_bytes(data)
    source.chmod(0o600)
    originals = tmp_path/'originals'
    originals.mkdir(mode=0o700)
    covered = CoveredFile('b'*32, 'c'*64, 'd'*32, len(data), hashlib.sha256(data).hexdigest())
    claim = SnapshotClaim(covered.version_id, covered.commitment_sha256, (covered,))
    prepared = artifacts.prepare_container((ContainerInput(source, covered.file_id),),
        output_dir=originals, namespace=ContainerNamespace('tenant', 'project', 'a'*32),
        policy=env.policies['origin'], trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        parent_versions=(claim.source_version,), snapshot=claim,
        limits=ContainerPreparationLimits(packing_limits=PackingLimits(
            target_bytes=25000, tolerance=.1, max_split_iterations=12, max_measure_calls=200)))
    assert len(prepared.volumes) > 1
    snapshot = PreparedSnapshot(prepared, claim)
    hashes = {v.path: v.path.read_bytes() for v in prepared.volumes}
    options = dict(authorized=True, policy=env.policies['controller'], trust_state_path=env.paths['controller'],
        gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    approved_dir = tmp_path/'approved'
    approved_dir.mkdir(mode=0o700)
    budget = _Budget(ContainerPreparationLimits())
    budget.reserve_temp(17)  # A caller's existing reservations survive this operation.
    result = approve_snapshot(snapshot, output_dir=approved_dir, _budget=budget, **options)
    assert budget.temporary == 17 and budget.crypto > len(prepared.volumes)
    assert result.claim == claim and result.prepared.logical_commitment_sha256 == prepared.logical_commitment_sha256
    assert len(result.prepared.volumes) == len(prepared.volumes)
    incoming = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with ExitStack() as stack:
        sources = {}
        for volume in result.prepared.volumes:
            sources[volume.key] = stack.enter_context(LocalFileRangeSource(volume.path))
            metadata = artifacts.inspect_container_metadata(sources[volume.key], **incoming)
            assert metadata.logical['snapshot'] == claim.record()
        extract_container_file(sources, covered.file_id, 'complete', authorized=True, **incoming)
    assert (Path(env.policies['receiver'].boundary)/'complete').read_bytes() == data

    local = env.snapshots['origin']
    context = CatalogContext('f'*32, env.paths['origin'], local.store_id, SCOPE, 'publisher',
        local.revision, local.epoch, env.publisher, (env.publisher,), env.homes['origin'],
        SECRET_REFERENCE, env.resolver)
    carrier = Carrier()
    def publication_authority():
        artifacts._current_authorities(env.paths['controller'], env.policies['controller'], include_controller=True)
    delivery = publish_prepared_job(result.prepared, job_root=tmp_path/'delivery', job_id='1'*32,
        destination_binding='2'*64, destinations={v.volume_id: carrier for v in result.prepared.volumes},
        authorization_check=publication_authority, local_context=context,
        publication_mode='content_addressed', **incoming)
    assert delivery.completed_volumes == len(prepared.volumes) == carrier.calls
    delivered_sources = {volume.key: BytesRangeSource(carrier.data[
        volume.key+'.sha256-'+volume.transport_sha256]) for volume in result.prepared.volumes}
    extract_container_file(delivered_sources, covered.file_id, 'delivered', authorized=True, **incoming)
    assert (Path(env.policies['receiver'].boundary)/'delivered').read_bytes() == data

    for label, altered in (
        ('missing-volume', replace(snapshot, prepared=replace(prepared, volumes=prepared.volumes[:-1]))),
        ('wrong-transport', replace(snapshot, prepared=replace(prepared, volumes=(
            replace(prepared.volumes[0], transport_sha256='0'*64), *prepared.volumes[1:])))),
        ('wrong-coverage', replace(snapshot, claim=SnapshotClaim(covered.version_id, 'e'*64,
            (replace(covered, commitment_sha256='e'*64),)))),
    ):
        output = tmp_path/label
        output.mkdir(mode=0o700)
        with pytest.raises((artifacts.ArtifactError, ContainerReadError)):
            approve_snapshot(altered, output_dir=output, _budget=budget, **options)
        assert not list(output.iterdir()) and budget.temporary == 17

    failed = tmp_path/'revoked'
    failed.mkdir(mode=0o700)
    marker = failed/'keep'
    marker.write_text('unrelated')
    gates = []

    def revoke_after_first_publication():
        if list(failed.glob('*.lssc')):
            gates.append(True)
            raise PermissionError('synthetic assignment changed')

    with pytest.raises(PermissionError):
        approve_snapshot(snapshot, output_dir=failed, _budget=budget,
            publication_check=revoke_after_first_publication, **options)
    assert gates and list(failed.iterdir()) == [marker] and budget.temporary == 17
    assert all(path.read_bytes() == value for path, value in hashes.items())
    assert not list(approved_dir.glob('.lssc-approve-set-*'))

    single = tmp_path/'single-revoked'
    single.mkdir(mode=0o700)

    def revoke_single():
        if list(single.glob('*.lssc')):
            raise PermissionError('synthetic assignment changed after approval publish')

    with LocalFileRangeSource(prepared.volumes[0].path) as stream:
        with pytest.raises(artifacts.ArtifactError):
            artifacts.approve_container_distribution(stream, output_dir=single,
                publication_check=revoke_single, _budget=budget,
                **{key: value for key, value in options.items() if key != 'authorized'})
    assert not list(single.iterdir()) and budget.temporary == 17
