"""Selection uses approved bytes and verified dependencies, never raw savings."""
from dataclasses import replace
import hashlib
from pathlib import Path
import random

import pytest

from ls.core.openpgp import artifacts
from ls.core.openpgp.container_delivery_selection import select_prepared_delivery, _delivery_cost
from ls.core.openpgp.container_delta_codec import create_patch
from ls.core.openpgp.container_delta_records import FileDelta
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReader, ContainerReadError
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_transfer import TransferError
from ls.tests.test_container_delta_codec import codec, evidence, private
from ls.tests.test_openpgp_artifacts import separate_machine_env, SECRET_REFERENCE, SCOPE
from ls.tests.test_content_addressed_publication import Carrier


@pytest.fixture
def alternatives(tmp_path, codec, separate_machine_env):
    env = separate_machine_env
    output = tmp_path/'carriers'; output.mkdir(mode=0o700)
    namespace = ContainerNamespace('tenant', 'project', 'a'*32)
    read_args = dict(policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
        gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    def prepare(inputs, **kwargs):
        value = artifacts.prepare_container(inputs, output_dir=output, namespace=namespace,
            policy=env.policies['origin'], trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver, **kwargs)
        approved = []
        for volume in value.volumes:
            with LocalFileRangeSource(volume.path) as source:
                approved.append(artifacts.approve_container_distribution(source, output_dir=output,
                    policy=env.policies['controller'], trust_state_path=env.paths['controller'],
                    gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver))
        return replace(value, volumes=tuple(approved))
    before = random.Random(12).randbytes(16000)
    after = before[:8000]+b'new content'+before[8011:]
    old, new = private(tmp_path/'old', before), private(tmp_path/'new', after)
    base = prepare((ContainerInput(old, '1'*32),))
    patch = tmp_path/'patch'
    claim = create_patch(old, new, patch, base=evidence(before), result=evidence(after),
                         policy=codec, budget=_Budget(ContainerPreparationLimits()))
    assert claim.length < len(after)
    change = FileDelta(base.version_id, base.logical_commitment_sha256, '1'*32,
                       evidence(before), claim, evidence(after), '2'*32)
    common = dict(artifact_id=base.artifact_id, parent_versions=(base.version_id,))
    unchanged = ContainerInput(private(tmp_path/'unchanged', b'unchanged file'), '5'*32)
    delta = prepare((ContainerInput(patch, '1'*32, delta=change), unchanged), **common)
    full = prepare((ContainerInput(new, '1'*32), unchanged), **common)
    reader = ContainerReader(**read_args, source_paths=tuple(v.path for v in base.volumes), delta_codec=codec,
                             limits=ContainerPreparationLimits(timeout_seconds=300))
    return full, delta, base, reader, env


def test_real_final_cost_availability_and_missing_base(alternatives, tmp_path):
    from ls.core.openpgp.container_publication_job import publish_prepared_job
    from ls.core.openpgp.container_catalog import CatalogContext
    full, delta, base, reader, env = alternatives
    carrier = Carrier()
    private_dependencies = tmp_path/'dependencies'; private_dependencies.mkdir(mode=0o700)
    for volume in base.volumes:
        volume.path.chmod(0o644)  # Shared encrypted carriers need private retained copies.
    args = dict(reader=reader, destination_for=lambda _v: carrier, authorization_check=lambda: None,
                publication_mode='content_addressed', dependency_output_dir=private_dependencies)
    budget = _Budget(reader.limits)
    selected = select_prepared_delivery(full, delta, _budget=budget, **args)
    assert selected.full_bytes == sum(v.path.stat().st_size for v in full.volumes)
    assert selected.delta_bytes == sum(v.path.stat().st_size for v in (*base.volumes, *delta.volumes))
    assert selected.prepared == full and selected.reason == 'full_not_larger'
    assert not selected.dependencies and budget.temporary == 0 and budget.crypto > 0
    for volume in base.volumes:
        carrier.data[volume.key+'.sha256-'+volume.transport_sha256] = volume.path.read_bytes()
    selected = select_prepared_delivery(full, delta, **args)
    assert selected.prepared == delta and selected.representation == 'delta'
    assert selected.delta_bytes == sum(v.serialized_length for v in delta.volumes)
    retained = selected.dependencies[0]
    assert retained.artifact_id == base.artifact_id and retained.logical_commitment_sha256 == base.logical_commitment_sha256
    assert retained.volumes[0].path.parent == private_dependencies
    assert retained.volumes[0].path.read_bytes() == base.volumes[0].path.read_bytes()
    assert retained.volumes[0].path.stat().st_mode & 0o777 == 0o600
    assert carrier.calls == 0
    local = env.snapshots['origin']
    context = CatalogContext('f'*32, env.paths['origin'], local.store_id, SCOPE, 'publisher',
        local.revision, local.epoch, env.publisher, (env.publisher,), env.homes['origin'], SECRET_REFERENCE, env.resolver)
    # The existing journal consumes the exact selection and its dependencies.
    # An earlier presence observation cannot authorize skipping a missing base.
    carrier.data.clear()
    published = publish_prepared_job(selected.prepared, dependencies=selected.dependencies,
        job_root=tmp_path/'job', job_id='3'*32, destination_binding='4'*64,
        destinations={v.volume_id: carrier for v in (*base.volumes, *delta.volumes)},
        authorization_check=lambda: None, local_context=context, policy=reader.policy,
        trust_state_path=reader.trust_state_path, gnupg_home=reader.gnupg_home,
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        publication_mode='content_addressed', limits=reader.limits)
    assert published.completed_volumes == published.total_volumes == 2 and carrier.calls == 2
    assert set(carrier.data.values()) == {v.path.read_bytes() for v in (*base.volumes, *delta.volumes)}
    for volume in full.volumes:
        carrier.data[volume.key+'.sha256-'+volume.transport_sha256] = volume.path.read_bytes()
    tied = select_prepared_delivery(full, delta, **args)
    assert tied.full_bytes == tied.delta_bytes == 0 and tied.prepared == full
    missing = select_prepared_delivery(full, delta, **(args | {'reader': replace(reader, source_paths=())}))
    assert missing.prepared == full and missing.reason == 'dependency_missing' and missing.delta_bytes is None
    assert not list(Path(reader.policy.boundary).glob('.lssc-select-*'))


def test_selection_rejects_replaced_ciphertext_and_authority(alternatives):
    full, delta, base, reader, env = alternatives
    carrier = Carrier()
    args = dict(reader=reader, destination_for=lambda _v: carrier, authorization_check=lambda: None,
                dependency_output_dir=full.volumes[0].path.parent)
    altered = replace(delta, volumes=(replace(delta.volumes[0], transport_sha256='0'*64),))
    with pytest.raises(ContainerReadError, match='changed'):
        select_prepared_delivery(full, altered, **args)
    def revoked():
        raise PermissionError('revoked')
    with pytest.raises(PermissionError):
        select_prepared_delivery(full, delta, **(args | {'authorization_check': revoked}))
    with pytest.raises(ContainerReadError, match='limit_exceeded'):
        select_prepared_delivery(full, delta, **(args | {'reader': replace(reader, max_sources=2)}))
    assert carrier.calls == 0
    assert not list(Path(reader.policy.boundary).glob('.lssc-select-*'))


def test_cost_rejects_bad_observations_and_charges_final_bytes(tmp_path):
    from ls.core.openpgp.container_preparation import PreparedVolume
    data = b'ciphertext-with-all-framing-and-approval'
    volume = PreparedVolume('opaque', private(tmp_path/'cipher', data), len(data),
                            hashlib.sha256(data).hexdigest(), '1'*32, 1)
    carrier = Carrier()
    budget, checks = _Budget(ContainerPreparationLimits()), []
    def cost(destination=carrier):
        return _delivery_cost((volume,), lambda _v: destination, 'conditional', budget, lambda: checks.append(True))
    assert cost() == len(data)
    carrier.data[volume.key] = data
    assert cost() == 0 and checks
    carrier.data[volume.key] = b'other ciphertext'
    with pytest.raises(TransferError, match='ciphertext_collision'):
        cost()
    class Offline:
        def observe(self, *_a, **_kw):
            raise TimeoutError
    with pytest.raises(TransferError, match='publish_uncertain'):
        cost(Offline())
    assert cost(volume.path) == 0
    assert cost(tmp_path/'absent') == len(data)
