"""Delta metadata binds exact inputs and cannot be mistaken for complete bytes."""
from dataclasses import replace
import copy
import hashlib
from pathlib import Path

import pytest

from ls.core.openpgp import artifact_records as records, artifacts
from ls.core.openpgp.container_delta_records import DELTA_FEATURE, FileDelta, FileEvidence, file_delta
from ls.core.openpgp.container_schema import UnsupportedRequiredFeature, validate_protected_schema
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_reconstruction import extract_container_file, ReconstructionError
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def claim():
    return FileDelta('b'*32, 'c'*64, 'd'*32, FileEvidence(1000, 'e'*64),
        FileEvidence(12, hashlib.sha256(b'opaque patch').hexdigest()),
        FileEvidence(2000, 'f'*64), '1'*32)


def test_delta_schema_preserves_legacy_and_requires_new_reader():
    old = records.schema_envelope()
    assert old['minor'] == 0 and DELTA_FEATURE not in old['required_features']
    supported = frozenset(old['required_features'])
    validate_protected_schema(old, supported_required_features=supported)
    new = records.schema_envelope(deltas=True)
    with pytest.raises(UnsupportedRequiredFeature, match=DELTA_FEATURE):
        validate_protected_schema(new, supported_required_features=supported)
    records.validate_schema(new)
    value = claim()
    file = {'file_id': '2'*32, 'length': value.result.length,
            'sha256': value.result.sha256, 'delta': value.record()}
    assert file_delta(file) == value
    for key, bad in [('codec', 'shell-command'), ('base_version', '0'*32),
                     ('base_commitment', 'bad'), ('change_id', 'bad')]:
        altered = copy.deepcopy(file); altered['delta'][key] = bad
        with pytest.raises(ValueError):
            file_delta(altered)


def test_authenticated_delta_inventory_and_explicit_unsupported_read(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    patch = tmp_path/'patch'; patch.write_bytes(b'opaque patch'); patch.chmod(0o600)
    output = tmp_path/'output'; output.mkdir(mode=0o700)
    delta = claim()
    prepared = artifacts.prepare_container((ContainerInput(patch, '2'*32, delta=delta),),
        output_dir=output, namespace=ContainerNamespace('tenant', 'project', 'a'*32),
        policy=env.policies['origin'], trust_state_path=env.paths['origin'],
        gnupg_home=env.homes['origin'], passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver, artifact_id='3'*32, parent_versions=(delta.base_version,))
    with LocalFileRangeSource(prepared.volumes[0].path) as source:
        approved = artifacts.approve_container_distribution(source, output_dir=output,
            policy=env.policies['controller'], trust_state_path=env.paths['controller'],
            gnupg_home=env.homes['controller'], passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.resolver)
    with LocalFileRangeSource(approved.path) as source:
        metadata = artifacts.inspect_container_metadata(source, policy=env.policies['receiver'],
            trust_state_path=env.paths['receiver'], gnupg_home=env.homes['receiver'],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
        assert file_delta(metadata.logical['files'][0]) == delta
        assert metadata.logical['files'][0]['length'] == 2000
        assert sum(m['length'] for v in metadata.inventory['volumes'] for m in v['messages']) == 12
        altered = copy.deepcopy(metadata.logical)
        altered['files'][0]['delta']['base_commitment'] = '9'*64
        assert records.logical_commitment(altered) != metadata.logical_commitment_sha256
        altered['parent_versions'] = []
        with pytest.raises(records.ArtifactRecordError):
            records.validate_logical(altered)
        monkeypatch.setattr(artifacts, '_copy_selected_file', lambda *_a, **_k: pytest.fail('patch released as file'))
        with pytest.raises(ReconstructionError, match='unsupported_delta'):
            extract_container_file({approved.key: source}, '2'*32, 'result', authorized=True,
                policy=env.policies['receiver'], trust_state_path=env.paths['receiver'],
                gnupg_home=env.homes['receiver'], passphrase_reference=SECRET_REFERENCE,
                secret_resolver=env.resolver)
    boundary = Path(env.policies['receiver'].boundary)
    assert not (boundary/'result').exists() and not list(boundary.glob('.lssc-file-*'))
    with pytest.raises(artifacts.ArtifactError):
        artifacts.prepare_container((ContainerInput(patch, '2'*32, delta=replace(delta,
            patch=FileEvidence(99, delta.patch.sha256))),), output_dir=output,
            namespace=ContainerNamespace('tenant', 'project', 'a'*32),
            policy=env.policies['origin'], trust_state_path=env.paths['origin'],
            gnupg_home=env.homes['origin'], passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.resolver, artifact_id='3'*32, parent_versions=(delta.base_version,))
