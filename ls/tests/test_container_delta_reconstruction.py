"""Authenticated base chains, exact bytes and dependency release guards."""
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from ls.core.openpgp import artifacts, container_reconstruction as reconstruction, trust_state
from ls.core.openpgp.container_delta_codec import create_patch
from ls.core.openpgp.container_delta_records import FileDelta
from ls.core.openpgp.container_delta_reconstruction import DeltaReconstructionPolicy
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.tests.test_container_delta_codec import codec, evidence, private
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def test_verified_two_change_chain_and_dependency_failures(tmp_path, codec, separate_machine_env, monkeypatch):
    env = separate_machine_env
    output = tmp_path/'volumes'; output.mkdir(mode=0o700)
    common = dict(trust_state_path=env.paths['receiver'], gnupg_home=env.homes['receiver'],
                  passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    policy = env.policies['receiver']
    boundary = Path(policy.boundary)
    artifact, file_id = '3'*32, '4'*32
    content = [b'base content\n'*4000]
    content += [content[0][:200]+b'first change'+content[0][210:]]
    content += [content[1][:2000]+b'second change'+content[1][2010:]]
    snapshots, versions, carriers = [], [], {}
    limits = ContainerPreparationLimits(timeout_seconds=120)
    for index, data in enumerate(content):
        snapshots.append(private(tmp_path/f'plain-{index}', data))
        delta = None
        source = snapshots[-1]
        if index:
            source = tmp_path/f'patch-{index}'
            patch = create_patch(snapshots[-2], snapshots[-1], source,
                base=evidence(content[index-1]), result=evidence(data), policy=codec, budget=_Budget(limits))
            assert patch is not None
            previous = versions[-1]
            delta = FileDelta(previous.version_id, previous.logical_commitment_sha256, file_id,
                evidence(content[index-1]), patch, evidence(data), str(index)*32)
        prepared = artifacts.prepare_container((ContainerInput(source, file_id, delta=delta),),
            output_dir=output, namespace=ContainerNamespace('tenant', 'project', 'a'*32),
            policy=env.policies['origin'], trust_state_path=env.paths['origin'],
            gnupg_home=env.homes['origin'], passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.resolver, artifact_id=artifact,
            parent_versions=(delta.base_version,) if delta else ())
        versions.append(prepared)
        carriers[prepared.version_id] = []
        for volume in prepared.volumes:
            with LocalFileRangeSource(volume.path) as stream:
                carriers[prepared.version_id].append(artifacts.approve_container_distribution(
                    stream, output_dir=output, policy=env.policies['controller'],
                    trust_state_path=env.paths['controller'], gnupg_home=env.homes['controller'],
                    passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver))

    @contextmanager
    def sources(version):
        with ExitStack() as stack:
            yield {item.key: stack.enter_context(LocalFileRangeSource(item.path))
                   for item in carriers.get(version, ())}

    resolved = []
    def resolve(metadata, delta, budget):
        budget.checkpoint()
        assert metadata.logical['artifact_id'] == artifact
        resolved.append(delta.base_version)
        return sources(delta.base_version)

    configured = DeltaReconstructionPolicy(codec, resolve)
    with sources(versions[-1].version_id) as selected:
        budget = _Budget(limits)
        receipt = reconstruction.extract_container_file(selected, file_id, 'complete', authorized=True,
            policy=policy, delta_policy=configured, limits=limits, _budget=budget, **common)
        assert (boundary/'complete').read_bytes() == content[-1]
        assert receipt.plaintext_sha256 == evidence(content[-1]).sha256
        assert resolved == [versions[1].version_id, versions[0].version_id]
        assert budget.temporary == len(content[-1])
        assert not list(boundary.glob('.lssc-file-*'))

        failures = [
            ('depth', replace(configured, max_chain_depth=1), 'delta_depth'),
            ('wrong-base', replace(configured, resolve_base=lambda *_: sources(versions[0].version_id)), 'base_mismatch'),
            ('missing', replace(configured, resolve_base=lambda *_: sources('missing')), 'invalid_input'),
        ]
        for name, configuration, error in failures:
            budget = _Budget(limits)
            with pytest.raises(reconstruction.ReconstructionError, match=error):
                reconstruction.extract_container_file(selected, file_id, name, authorized=True,
                    policy=policy, delta_policy=configuration, limits=limits, _budget=budget, **common)
            assert budget.temporary == 0
            assert not (boundary/name).exists() and not list(boundary.glob('.lssc-file-*'))

        record = trust_state._record_accepted_container
        checked = []
        def reject_dependency(path, **kwargs):
            required = kwargs.get('required_selected_receipts', ())
            if len(required) == 3:
                checked.append(required)
                kwargs['required_selected_receipts'] = ('f'*64, *required[1:])
            return record(path, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(trust_state, '_record_accepted_container', reject_dependency)
            budget = _Budget(limits)
            with pytest.raises(artifacts.ArtifactError):
                reconstruction.extract_container_file(selected, file_id, 'lost-receipt', authorized=True,
                    policy=policy, delta_policy=configured, limits=limits, _budget=budget, **common)
            assert len(checked) == 1 and budget.temporary == 0
            assert not (boundary/'lost-receipt').exists() and not list(boundary.glob('.lssc-file-*'))

        # A dependency that ceases to satisfy admission during decoding cannot
        # be hidden behind the freshness of the requested version.
        changed = [False]
        original_apply, original_policy = reconstruction.apply_patch, artifacts._policy_logical
        def decode(*args, **kwargs):
            value = original_apply(*args, **kwargs)
            changed[0] = True
            return value
        def admission(logical, *args, **kwargs):
            if changed[0] and logical['version_id'] == versions[0].version_id:
                raise artifacts.ArtifactError(artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED)
            return original_policy(logical, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(reconstruction, 'apply_patch', decode)
            patch.setattr(artifacts, '_policy_logical', admission)
            budget = _Budget(limits)
            with pytest.raises(artifacts.ArtifactError):
                reconstruction.extract_container_file(selected, file_id, 'stale-base', authorized=True,
                    policy=policy, delta_policy=configured, limits=limits, _budget=budget, **common)
            assert budget.temporary == 0
            assert not (boundary/'stale-base').exists() and not list(boundary.glob('.lssc-file-*'))
