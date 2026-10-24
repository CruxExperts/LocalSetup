"""Exact worker results, serialized admission, delayed delivery and downgrade refusal."""
import copy
from dataclasses import asdict, replace
import hashlib
from pathlib import Path

import pytest

from ls.core.live_configuration import ConfigurationError, _decode, _encode
from ls.core.openpgp import artifacts, container_runtime as runtime
from ls.core.openpgp.artifact_records import logical_commitment, schema_envelope
from ls.core.openpgp.container_consolidation import prepare_snapshot, approve_snapshot
from ls.core.openpgp.container_preparation import ContainerNamespace, ContainerPreparationLimits, _Budget
from ls.core.openpgp.container_reader import ContainerReader
from ls.core.openpgp.container_reconstruction import extract_container_file
from ls.core.openpgp.container_schema import UnsupportedRequiredFeature, validate_protected_schema
from ls.core.openpgp.container_snapshot_records import CoveredFile, SnapshotClaim, snapshot_claim
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_worker_authority import WorkerAuthorityError
from ls.core.openpgp.container_worker_records import (
    WorkerSnapshotProof, bare_result_digest, validate_worker_layers, worker_snapshot_proof,
)
from ls.tests.test_agent_container_reads import prepare
from ls.tests.test_container_worker_authority import configured_worker
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, separate_machine_env


def test_worker_record_bounds_declarations_and_exact_binding():
    covered = CoveredFile('1'*32, '2'*64, '3'*32, 4, '5'*64)
    legacy = SnapshotClaim(covered.version_id, covered.commitment_sha256, (covered,))
    assert set(legacy.record()) == {'source_version', 'source_commitment', 'covered'}
    assert snapshot_claim(legacy.record()).record() == legacy.record()
    proof = WorkerSnapshotProof(b'opaque encrypted signed proof')
    claim = replace(legacy, worker=proof)
    assert snapshot_claim(claim.record()) == claim
    with pytest.raises(ValueError):
        WorkerSnapshotProof(b'x'*(32*1024+1))
    for invalid in ({'request': '!!!'}, {'request': 'Zh=='}, {'request': ''},
                    {'request': 'A'*50000}, {'request': 'YQ==', 'extra': 1}):
        with pytest.raises(ValueError):
            worker_snapshot_proof(invalid)
    old_features = frozenset(schema_envelope(snapshot=True)['required_features'])
    schema = schema_envelope(snapshot=True, worker_snapshot=True)
    with pytest.raises(UnsupportedRequiredFeature):
        validate_protected_schema(schema, supported_required_features=old_features)
    logical = dict(artifact_id='6'*32, version_id='7'*32, origin={'fingerprint': 'A'*40},
        snapshot=claim.record(), files=[{'provenance': {'name': 'first'}}], parent_versions=['1'*32])
    marker = dict(version=1, worker='B'*40, assignment='8'*64, job='9'*32,
        destination='a'*64, request=hashlib.sha256(proof.request).hexdigest(), result=bare_result_digest(logical))
    origin = dict(schema=schema, logical=logical)
    outer = dict(schema=schema_envelope(worker_snapshot=True), worker_admission=marker)
    check = lambda record, wrapper: validate_worker_layers(record, wrapper, 'C'*40, allow_origin_unapproved=False)
    check(origin, outer)
    for key, value in (('version_id', '8'*32), ('artifact_id', '9'*32), ('parent_versions', ['4'*32]),
                       ('files', [{'provenance': {'name': 'different'}}])):
        with pytest.raises(ValueError):
            check(dict(origin, logical=dict(logical, **{key: value})), outer)
    for wrapper in (None, {'schema': outer['schema']},
                    dict(outer, schema=schema_envelope()),
                    dict(outer, worker_admission=dict(marker, worker='A'*40))):
        with pytest.raises(ValueError):
            check(origin, wrapper)
    for replacement in (schema_envelope(snapshot=True), dict(schema, minor=2)):
        with pytest.raises(ValueError):
            check(dict(origin, schema=replacement), outer)
    changed = copy.deepcopy(origin)
    changed['logical']['snapshot']['covered'][0]['sha256'] = 'b'*64
    with pytest.raises(ValueError):
        check(changed, outer)
    validate_worker_layers(origin, None, 'C'*40, allow_origin_unapproved=True)


def test_real_controller_admission_serializes_reassignment_and_preserves_history(tmp_path, separate_machine_env):
    env = separate_machine_env
    content = b'independently worker signed snapshot'
    base = prepare(env, tmp_path, (content,))[0]
    incoming = dict(policy=replace(env.policies['receiver'], require_worker_snapshots=True),
        trust_state_path=env.paths['receiver'], gnupg_home=env.homes['receiver'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver)
    with LocalFileRangeSource(base.path) as stream:
        metadata = artifacts.inspect_container_metadata(stream, **incoming)
    store, initial, view, actor, endpoint, worker = configured_worker(env, tmp_path/'assignments',
                                                                    metadata.logical['artifact_id'])
    limits = ContainerPreparationLimits(timeout_seconds=300)
    budget = _Budget(limits)
    reader = ContainerReader(**incoming, source_paths=(base.path,), limits=limits)
    output = tmp_path/'prepared'; output.mkdir(mode=0o700)
    snapshot = prepare_snapshot(base.path, reader=reader, authorized=True, output_dir=output,
        namespace=ContainerNamespace('tenant', 'project', 'a'*32), publication_policy=env.policies['origin'],
        trust_state_path=env.paths['origin'], gnupg_home=env.homes['origin'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver, _budget=budget,
        worker_authority=worker, job_id='3'*32, destination_binding='4'*64)
    assert snapshot.claim.worker is not None
    approved_dir = tmp_path/'approved'; approved_dir.mkdir(mode=0o700)
    options = dict(authorized=True, output_dir=approved_dir, policy=env.policies['controller'],
        trust_state_path=env.paths['controller'], gnupg_home=env.homes['controller'],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver, limits=limits, _budget=budget)
    with pytest.raises(artifacts.ArtifactError):
        approve_snapshot(snapshot, **options)
    assert not list(approved_dir.iterdir())
    with LocalFileRangeSource(snapshot.prepared.volumes[0].path) as stream:
        with pytest.raises(artifacts.ArtifactError):
            artifacts.inspect_container_metadata(stream, **incoming)
        prepared_metadata = artifacts.inspect_container_metadata(stream, **incoming, allow_origin_unapproved=True)
    guard_args = dict(assignment_binding=worker.binding, job_id='3'*32,
        source_commitment=metadata.logical_commitment_sha256, destination_binding='4'*64,
        policy=env.policies['controller'], budget=budget)
    # A valid content-publisher signature cannot impersonate the assigned worker.
    request = _decode(actor.open(snapshot.claim.worker.request))
    publisher = replace(actor, signer=env.publisher, home=env.homes['origin'])
    forged = copy.deepcopy(prepared_metadata.logical)
    forged['snapshot']['worker'] = WorkerSnapshotProof(publisher.seal(_encode(request))).record()
    with endpoint.approval_guard(**guard_args) as signer_guard:
        with pytest.raises(ConfigurationError):
            signer_guard.receipt(replace(prepared_metadata, logical=forged,
                logical_commitment_sha256=logical_commitment(forged)), options['policy'], budget)
    with endpoint.approval_guard(**(guard_args | {'job_id': '5'*32})) as wrong:
        with pytest.raises(artifacts.ArtifactError):
            approve_snapshot(snapshot, **options, _worker_admission=wrong)
    assert not list(approved_dir.iterdir())
    blocked = []
    def competing_reassignment():
        if not blocked:
            with pytest.raises(ConfigurationError, match='configuration_busy'):
                store.commit(initial.record['values'], expected_digest=initial.digest, authorized=True)
            blocked.append(True)
    with endpoint.approval_guard(**guard_args) as guard:
        approved = approve_snapshot(snapshot, **options, _worker_admission=guard,
                                    publication_check=competing_reassignment)
    assert blocked
    with pytest.raises(WorkerAuthorityError, match='worker_admission_closed'):
        guard.check()
    store.commit(initial.record['values'], expected_digest=initial.digest, authorized=True)
    with pytest.raises(WorkerAuthorityError, match='assignment_changed'):
        with endpoint.approval_guard(**guard_args):
            pytest.fail('stale worker was admitted')
    # Delayed receipt of an already admitted immutable result is valid history.
    volume = approved.prepared.volumes[0]
    exact = volume.path.read_bytes()
    with LocalFileRangeSource(volume.path) as stream:
        accepted = artifacts.inspect_container_metadata(stream, **incoming)
        assert len(accepted.metadata_layers) == 2
        assert accepted.logical['snapshot'] == snapshot.claim.record()
        extract_container_file({volume.key: stream}, metadata.logical['files'][0]['file_id'],
                               'accepted-worker-result', authorized=True, **incoming)
    assert (Path(reader.policy.boundary)/'accepted-worker-result').read_bytes() == content
    assert volume.path.read_bytes() == exact and budget.temporary == 0
    with LocalFileRangeSource(volume.path) as stream:
        artifacts.open_historical_container_message(stream, 0, 'historical-worker-result',
                                                    authorized=True, **incoming)
    assert (Path(reader.policy.boundary)/'historical-worker-result').read_bytes() == content

    # Requiring worker admission cannot silently accept an unfenced legacy snapshot.
    legacy_logical = copy.deepcopy(accepted.logical)
    del legacy_logical['snapshot']['worker']
    with pytest.raises(artifacts.ArtifactError):
        artifacts._policy_logical(legacy_logical, incoming['policy'], historical=False)
    artifacts._policy_logical(legacy_logical, env.policies['receiver'], historical=False)
    artifacts._policy_logical(legacy_logical, incoming['policy'], historical=True)
    assert artifacts.container_admission_policy_sha256(incoming['policy']) != artifacts.container_admission_policy_sha256(env.policies['receiver'])

    fields = {k: v for k, v in asdict(env.policies['receiver']).items()
              if k not in ('content_revision', 'created_at', 'expires_at')}
    discovery = artifacts.ArtifactDiscoveryPolicy(**fields, admit_from=0, admit_until=env.policies['receiver'].expires_at,
        max_content_age_seconds=86400, max_content_lifetime_seconds=30*86400)
    config = runtime.RuntimeConfig('a'*32, read_policy=discovery,
        remote_content=runtime.RemoteContentTrust(env.paths['receiver'], discovery.trust_store_id, discovery.scope))
    old = runtime._config_dict(config)
    assert old['version'] == 3 and 'require_worker_snapshots' not in old['read_policy']
    assert runtime._config_dict(runtime._parse_config(old, config.runtime_id)) == old
    required = replace(config, read_policy=replace(discovery, require_worker_snapshots=True))
    new = runtime._config_dict(required)
    assert new['version'] == 4 and runtime._parse_config(new, config.runtime_id) == required
    with pytest.raises(runtime.RuntimeErrorCode):
        runtime._parse_config(dict(new, version=3), config.runtime_id)
