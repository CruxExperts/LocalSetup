from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from ls.core.openpgp import (
    ArtifactDiscoveryPolicy, ArtifactError, ArtifactErrorCode, ArtifactPolicy,
    ArtifactProvenance,
    ContainerInput, ContainerNamespace, ContainerPreparationLimits,
    SecretProvider, SecretReference, SecretResolver, initialize_trust_state,
    prepare_container, approve_container_distribution,
    inspect_container_metadata, accept_container_metadata,
    extract_container_message, open_historical_container_message,
    is_artifact_payload, metadata_observation_unchanged,
)
from ls.core.openpgp import artifacts as artifacts_api
from ls.core.openpgp import container_preparation
from ls.core.openpgp import publishing_transition, transition, trust_state
from ls.core.openpgp.container_format import ContainerLimits, read_metadata, copy_message, write_container
from ls.core.openpgp.container_packing import PackingLimits
from ls.core.openpgp.container_sources import BytesRangeSource, LocalFileRangeSource
from ls.core.openpgp.envelope_io import FileEnvelopeLimits
from ls.core.openpgp.artifact_records import (
    ArtifactRecordError, logical_commitment, parse_descriptor, validate_inventory,
)
from ls.core.openpgp.contracts import EnvelopePolicy, LocalTrust
from ls.core.openpgp.opening import open_envelope
from ls.core.openpgp.envelope import seal_envelope

from ls.tests.openpgp_key_helpers import (
    PASSPHRASE, _gpg, _create_key, _register_gpg_home_cleanup,
)
SECRET_NAME = "LS_OPENPGP_ARTIFACT_TEST_SECRET"
SECRET_REFERENCE = SecretReference(SecretProvider.ENV, SECRET_NAME)
SCOPE = ("agentq:fixture",)
PURPOSE = "release.snapshot"


def _private_fingerprints(home: Path) -> set[str]:
    primaries: set[str] = set()
    await_primary = False
    for line in _gpg(home, "--with-colons", "--list-secret-keys").decode().splitlines():
        if line.startswith("sec:"):
            await_primary = True
        elif await_primary and line.startswith("fpr:"):
            primaries.add(line.split(":")[9])
            await_primary = False
    return primaries


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest):
    if shutil.which("gpg") is None:
        pytest.skip("GnuPG is required for disposable cryptographic fixtures")
    home = tmp_path_factory.mktemp("artifact-gpg-home")
    _register_gpg_home_cleanup(request, home)
    home.chmod(0o700)
    owner, owner_certificate = _create_key(home, "Owner")
    publisher, publisher_certificate = _create_key(home, "Publisher")
    return SimpleNamespace(
        home=home,
        owner=owner,
        owner_certificate=owner_certificate,
        publisher=publisher,
        publisher_certificate=publisher_certificate,
    )


def test_shared_key_fixture_has_only_its_disposable_keys(keys) -> None:
    assert _private_fingerprints(keys.home) == {keys.owner, keys.publisher}


@pytest.fixture
def artifact_env(tmp_path: Path, keys, monkeypatch: pytest.MonkeyPatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    boundary = private / "accepted"
    boundary.mkdir(mode=0o700)
    path = private / "trust.db"

    # These disposable 2048-bit keys exercise artifact/envelope behavior; the
    # lifecycle suite separately covers certificate and production-key policy.
    fingerprints = {
        keys.owner_certificate: keys.owner,
        keys.publisher_certificate: keys.publisher,
    }
    monkeypatch.setattr(
        trust_state,
        "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(
            primary_fingerprint=fingerprints[certificate]
        ),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)
    initial = initialize_trust_state(
        path,
        scope=SCOPE,
        owner_certificate=keys.owner_certificate,
        publisher_certificate=keys.publisher_certificate,
    )

    monkeypatch.setattr(artifacts_api, "_clock", lambda: int(__import__("time").time()))
    resolver = SecretResolver(env_values={SECRET_NAME: PASSPHRASE})
    now = int(__import__("time").time())
    policy = ArtifactPolicy(
        trust_store_id=initial.store_id,
        trust_revision=initial.revision,
        governance_epoch=initial.epoch,
        scope=SCOPE,
        origin_fingerprint=keys.publisher,
        audience=tuple(sorted((keys.owner, keys.publisher))),
        purpose=PURPOSE,
        content_revision="commit:1234abcd",
        created_at=now,
        expires_at=now + 3600,
        boundary=boundary,
        controller_fingerprint=keys.owner,
    )
    return SimpleNamespace(
        path=path,
        boundary=boundary,
        home=keys.home,
        resolver=resolver,
        initial=initial,
        owner=keys.owner,
        publisher=keys.publisher,
        policy=policy,
    )


@pytest.fixture
def separate_machine_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
):
    if shutil.which("gpg") is None:
        pytest.skip("GnuPG is required for disposable cryptographic fixtures")
    homes = {}
    for role in ("origin", "controller", "receiver"):
        home = tmp_path / role / "gnupg"
        home.parent.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        _register_gpg_home_cleanup(request, home)
        homes[role] = home
    publisher, publisher_cert = _create_key(homes["origin"], "Cross Publisher")
    owner, owner_cert = _create_key(homes["controller"], "Cross Owner")
    recipient, recipient_cert = _create_key(homes["receiver"], "Cross Recipient")
    for home in homes.values():
        for cert in (publisher_cert, owner_cert, recipient_cert):
            _gpg(home, "--import", input_data=cert)
    assert _private_fingerprints(homes["origin"]) == {publisher}
    assert _private_fingerprints(homes["controller"]) == {owner}
    assert _private_fingerprints(homes["receiver"]) == {recipient}

    fingerprints = {publisher_cert: publisher, owner_cert: owner}
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(
            primary_fingerprint=fingerprints[certificate]
        ),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)
    resolver = SecretResolver(env_values={SECRET_NAME: PASSPHRASE})
    audience = tuple(sorted((owner, recipient)))
    now = int(__import__("time").time())
    paths = {}
    snapshots = {}
    policies = {}
    for role in homes:
        private = tmp_path / role / "private"
        private.mkdir(mode=0o700)
        boundary = private / "accepted"
        boundary.mkdir(mode=0o700)
        path = private / "trust.db"
        snapshot = initialize_trust_state(
            path,
            scope=SCOPE,
            owner_certificate=owner_cert,
            publisher_certificate=publisher_cert,
        )
        paths[role] = path
        snapshots[role] = snapshot
        policies[role] = ArtifactPolicy(
            trust_store_id=snapshot.store_id,
            trust_revision=snapshot.revision,
            governance_epoch=snapshot.epoch,
            scope=SCOPE,
            origin_fingerprint=publisher,
            audience=audience,
            purpose=PURPOSE,
            content_revision="commit:cross-machine",
            created_at=now,
            expires_at=now + 3600,
            boundary=boundary,
            controller_fingerprint=owner,
        )
    # Fixture-only revision advance models an independently managed local store.
    with sqlite3.connect(paths["receiver"]) as db:
        db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
    receiver_snapshot = trust_state.load_trust_state(paths["receiver"], expected_scope=SCOPE)
    policies["receiver"] = replace(
        policies["receiver"],
        trust_revision=receiver_snapshot.revision,
        origin_trust_store_id=snapshots["origin"].store_id,
        origin_trust_revision=snapshots["origin"].revision,
        origin_governance_epoch=snapshots["origin"].epoch,
    )
    policies["controller"] = replace(
        policies["controller"],
        origin_trust_store_id=snapshots["origin"].store_id,
        origin_trust_revision=snapshots["origin"].revision,
        origin_governance_epoch=snapshots["origin"].epoch,
    )
    assert len({snap.store_id for snap in snapshots.values()}) == 3
    assert policies["receiver"].trust_revision != policies["origin"].trust_revision
    return SimpleNamespace(
        homes=homes, paths=paths, snapshots=snapshots, policies=policies,
        resolver=resolver, owner=owner, publisher=publisher, recipient=recipient,
    )


def _prepare(env, tmp_path: Path, *, content: bytes = b"secret\x00bytes"):
    output = tmp_path / "origin" / "volumes"
    output.mkdir(mode=0o700)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    namespace = ContainerNamespace("test-tenant", "test-project", "a" * 32)
    prepared = prepare_container(
        (ContainerInput(source, "b" * 32, ArtifactProvenance(original_name="private.bin")),),
        output_dir=output, namespace=namespace,
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    assert len(prepared.volumes) == 1
    return prepared


def test_prepare_rejects_invalid_optional_lineage_before_crypto(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "lineage.bin"
    source.write_bytes(b"lineage")
    output = tmp_path / "lineage-volumes"
    output.mkdir(mode=0o700)
    input_file = ContainerInput(source, "b" * 32)
    calls = []
    original_scan = container_preparation._scan_file

    def observe_scan(*args, **kwargs):
        calls.append(True)
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(container_preparation, "_scan_file", observe_scan)
    for artifact_id, parent_versions in (
        ("not-opaque", ()),
        (None, ("a" * 32, "a" * 32)),
        (None, tuple("a" * 32 for _ in range(65))),
        (None, ["a" * 32]),
    ):
        with pytest.raises(ArtifactError) as raised:
            prepare_container(
                (input_file,), output_dir=output,
                namespace=ContainerNamespace("tenant", "project", "a" * 32),
                policy=env.policies["origin"], trust_state_path=env.paths["origin"],
                gnupg_home=env.homes["origin"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                artifact_id=artifact_id, parent_versions=parent_versions,
            )
        assert raised.value.code is ArtifactErrorCode.INVALID_ARTIFACT
    assert calls == []
    assert list(output.iterdir()) == []


def test_separate_homes_metadata_selected_receipts_and_history(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    assert _private_fingerprints(env.homes["receiver"]) == {env.recipient}
    origin = prepared.volumes[0]
    with LocalFileRangeSource(origin.path) as source:
        approved = approve_container_distribution(
            source, output_dir=origin.path.parent,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert approved.key != origin.key
    receiver_policy = env.policies["receiver"]
    with LocalFileRangeSource(approved.path) as source:
        metadata = inspect_container_metadata(
            source, policy=receiver_policy, trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert metadata.payload_verified is False
        assert metadata.logical_commitment_sha256 == prepared.logical_commitment_sha256
        receipt = accept_container_metadata(
            source, policy=receiver_policy, trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert receipt.status == "accepted"
        with pytest.raises(ArtifactError):
            open_historical_container_message(
                source, 0, "before-selected.bin", authorized=True,
                policy=receiver_policy, trust_state_path=env.paths["receiver"],
                gnupg_home=env.homes["receiver"], expected_key=approved.key,
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            )
        selected = extract_container_message(
            source, 0, "selected.bin", authorized=True,
            policy=receiver_policy, trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert selected.status == "accepted"
        assert (Path(receiver_policy.boundary) / "selected.bin").read_bytes() == b"secret\x00bytes"
        monkeypatch.setattr(artifacts_api, "_clock", lambda: receiver_policy.expires_at + 1)
        monkeypatch.setattr(trust_state, "_clock", lambda: receiver_policy.expires_at + 1)
        historical = open_historical_container_message(
            source, 0, "historical.bin", authorized=True,
            policy=receiver_policy, trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert historical.status == "opened"
        assert (Path(receiver_policy.boundary) / "historical.bin").read_bytes() == b"secret\x00bytes"


def test_metadata_read_never_fetches_payload(separate_machine_env, tmp_path: Path) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    volume = prepared.volumes[0]
    with LocalFileRangeSource(volume.path) as source:
        header = read_metadata(source)
        class Spy:
            @property
            def size(self):
                return source.size
            def read_range(self, offset, length):
                assert offset + length <= header.messages_offset + len(b"messages:\n")
                return source.read_range(offset, length)
        metadata = inspect_container_metadata(
            Spy(), policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
        assert metadata.payload_verified is False
        assert metadata.volume_record["messages"][0]["file_id"] == "b" * 32


def test_public_guard_recognizes_canonical_format() -> None:
    assert is_artifact_payload(b"format: localsetup.secure-container\nversion: 1\n")
    assert not is_artifact_payload(b"ordinary plaintext")


def test_whole_file_above_byte_api_limit_stays_one_segment(
    separate_machine_env, tmp_path: Path,
) -> None:
    env = separate_machine_env
    source_path = tmp_path / "twenty-meg.bin"
    with source_path.open("wb") as handle:
        for _ in range(20):
            handle.write(b"0123456789abcdef" * (1024 * 1024 // 16))
    output = tmp_path / "volumes"
    output.mkdir(mode=0o700)
    prepared = prepare_container(
        (ContainerInput(source_path, "c" * 32),),
        output_dir=output,
        namespace=ContainerNamespace("tenant", "project", "d" * 32),
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    assert len(prepared.volumes) == 1
    volume = prepared.volumes[0]
    assert volume.serialized_length <= 110_000_000
    with LocalFileRangeSource(volume.path) as origin_source:
        inspected = inspect_container_metadata(
            origin_source, policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
        assert len(inspected.volume_record["messages"]) == 1
        assert inspected.volume_record["messages"][0]["segment_count"] == 1
        approved = approve_container_distribution(
            origin_source, output_dir=output,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    with LocalFileRangeSource(approved.path) as approved_source:
        receipt = extract_container_message(
            approved_source, 0, "large-output.bin", authorized=True,
            policy=env.policies["receiver"],
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert receipt.status == "accepted"
    assert hashlib.sha256((Path(env.policies["receiver"].boundary) / "large-output.bin").read_bytes()).hexdigest() == hashlib.sha256(source_path.read_bytes()).hexdigest()


def test_multifile_global_inventory_and_actual_split_ceiling(
    separate_machine_env, tmp_path: Path,
) -> None:
    env = separate_machine_env
    small = tmp_path / "small.bin"
    large = tmp_path / "large.bin"
    small.write_bytes(b"small-" * 300)
    large.write_bytes(bytes(range(256)) * 120)
    output = tmp_path / "packed"
    output.mkdir(mode=0o700)
    limits = ContainerPreparationLimits(
        packing_limits=PackingLimits(
            target_bytes=25_000, tolerance=0.10,
            max_split_iterations=12, max_measure_calls=200,
        ),
        max_plan_passes=8,
    )
    prepared = prepare_container(
        (ContainerInput(small, "e" * 32), ContainerInput(large, "f" * 32)),
        output_dir=output,
        namespace=ContainerNamespace("tenant", "project", "a" * 32),
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        limits=limits,
    )
    assert len(prepared.volumes) >= 2
    assert all(volume.serialized_length <= 27_500 for volume in prepared.volumes)
    assert all(volume.path.stat().st_size == volume.serialized_length
               for volume in prepared.volumes)
    inventories = []
    for volume in prepared.volumes:
        with LocalFileRangeSource(volume.path) as source:
            metadata = inspect_container_metadata(
                source, policy=env.policies["controller"],
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                allow_origin_unapproved=True,
            )
        inventories.append(metadata.inventory)
    assert inventories[0] == inventories[-1]
    assert len(inventories[0]["volumes"]) == len(prepared.volumes)
    segments = [item for volume in inventories[0]["volumes"]
                for item in volume["messages"] if item["file_id"] == "f" * 32]
    assert len(segments) > 1
    assert sum(item["length"] for item in segments) == large.stat().st_size


def _discovery(exact: ArtifactPolicy) -> ArtifactDiscoveryPolicy:
    return ArtifactDiscoveryPolicy(
        trust_store_id=exact.trust_store_id,
        trust_revision=exact.trust_revision,
        governance_epoch=exact.governance_epoch,
        scope=exact.scope,
        origin_fingerprint=exact.origin_fingerprint,
        audience=exact.audience,
        purpose=exact.purpose,
        boundary=exact.boundary,
        controller_fingerprint=exact.controller_fingerprint,
        origin_trust_store_id=exact.origin_trust_store_id,
        origin_trust_revision=exact.origin_trust_revision,
        origin_governance_epoch=exact.origin_governance_epoch,
        admit_from=exact.created_at - 60,
        admit_until=exact.expires_at + 60,
        max_content_age_seconds=3600,
        max_content_lifetime_seconds=3600,
    )


def test_discovery_policy_learns_only_verified_version_with_bounded_admission(
    separate_machine_env, tmp_path: Path,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path, content=b"new-version")
    origin = prepared.volumes[0]
    discovery_controller = _discovery(env.policies["controller"])
    discovery_receiver = _discovery(env.policies["receiver"])
    with LocalFileRangeSource(origin.path) as source:
        with pytest.raises(ArtifactError) as exact_error:
            inspect_container_metadata(
                source,
                policy=replace(env.policies["controller"], content_revision="commit:other"),
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                allow_origin_unapproved=True,
            )
        assert exact_error.value.code is ArtifactErrorCode.AUTHORIZATION_DENIED
        discovered = inspect_container_metadata(
            source, policy=discovery_controller,
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
        assert discovered.logical["content_revision"] == "commit:cross-machine"
        approved = approve_container_distribution(
            source, output_dir=origin.path.parent,
            policy=discovery_controller,
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    with LocalFileRangeSource(approved.path) as source:
        receipt = extract_container_message(
            source, 0, "discovered.bin", authorized=True,
            policy=discovery_receiver,
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert receipt.status == "accepted"
    assert (Path(discovery_receiver.boundary) / "discovered.bin").read_bytes() == b"new-version"


def test_unknown_outer_and_protected_features_report_unsupported(
    separate_machine_env, tmp_path: Path,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    volume = prepared.volumes[0]
    policy = env.policies["controller"]
    raw = volume.path.read_bytes()
    changed_outer = raw.replace(b"version: 1\n", b"version: 2\n", 1)
    with pytest.raises(ArtifactError) as outer_error:
        inspect_container_metadata(
            BytesRangeSource(changed_outer), policy=policy,
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
    assert outer_error.value.code is ArtifactErrorCode.UNSUPPORTED
    with LocalFileRangeSource(volume.path) as source:
        header = read_metadata(source)
        message = io.BytesIO()
        metadata = inspect_container_metadata(
            source, policy=policy, trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
        copy_message(source, header,
                     parse_descriptor(metadata.volume_record["messages"][0]["descriptor"]),
                     message)
    plaintext = open_envelope(
        header.encrypted_metadata, gnupg_home=env.homes["controller"],
        policy=EnvelopePolicy(expected_signers=(env.publisher,),
                              expected_recipients=policy.audience),
        local_trust=LocalTrust({env.publisher}),
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    record = json.loads(plaintext)
    for change in (lambda schema: schema.update({"major": 2}),
                   lambda schema: schema.update({"required_features": ["unknown-future-feature"]})):
        mutated = json.loads(json.dumps(record))
        change(mutated["schema"])
        new_cipher = seal_envelope(
            json.dumps(mutated, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode(),
            gnupg_home=env.homes["origin"], signer_fingerprint=env.publisher,
            recipient_fingerprints=policy.audience,
            local_trust=LocalTrust({env.publisher}),
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            disable_compression=True,
        )
        rewritten = io.BytesIO()
        write_container(rewritten, recipient_hints=policy.audience,
                        metadata_ciphertext=new_cipher, messages=(message.getvalue(),))
        with pytest.raises(ArtifactError) as protected_error:
            inspect_container_metadata(
                BytesRangeSource(rewritten.getvalue()), policy=policy,
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                allow_origin_unapproved=True,
            )
        assert protected_error.value.code is ArtifactErrorCode.UNSUPPORTED


def test_inventory_rejects_other_volume_namespace_escape() -> None:
    artifact_id, version_id = "1" * 32, "2" * 32
    first, second = "3" * 32, "4" * 32
    file_id = "5" * 32
    logical = {
        "artifact_id": artifact_id, "version_id": version_id, "parent_versions": [],
        "origin": {"store_id": "a" * 64, "revision": 1, "epoch": 1,
                   "fingerprint": "A" * 40},
        "audience": ["B" * 40], "scope": ["test:scope"],
        "purpose": "test.purpose", "content_revision": "v1",
        "created_at": 1, "expires_at": 2,
        "files": [{"file_id": file_id, "length": 2, "sha256": "b" * 64,
                   "provenance": {"original_name": None, "original_path": None,
                                  "source_revision": None, "source_hashes": [],
                                  "operational_metadata": {}}}],
    }
    def segment(index: int):
        return {"file_id": file_id, "offset": index, "length": 1,
                "segment_index": index, "segment_count": 2,
                "plaintext_sha256": "c" * 64,
                "descriptor": {"message_index": 0, "offset": 10,
                               "serialized_length": 24, "ciphertext_length": 1,
                               "sha256": "d" * 64}}
    inventory = {"volumes": [
        {"volume_id": first,
         "key": f"lssc/v1/tenant/project/{'6'*32}/{artifact_id}/{version_id}/{first}.lssc",
         "messages": [segment(0)]},
        {"volume_id": second,
         "key": f"lssc/v1/tenant/project/{'6'*32}/{artifact_id}/{version_id}/{second}.lssc",
         "messages": [segment(1)]},
    ]}
    validate_inventory(inventory, logical)
    for bound in (ContainerLimits(max_volumes=1), ContainerLimits(max_total_segments=1)):
        with pytest.raises(ArtifactRecordError):
            validate_inventory(inventory, logical, limits=bound)
    validate_inventory(inventory, logical, limits=ContainerLimits(max_volumes=2, max_total_segments=2))
    two_files = {**logical, "files": [*logical["files"], {**logical["files"][0], "file_id": "9" * 32}]}
    with pytest.raises(ArtifactRecordError):
        logical_commitment(two_files, limits=ContainerLimits(max_files=1))
    assert logical_commitment(two_files, limits=ContainerLimits(max_files=2)) == logical_commitment(two_files)
    inventory["volumes"][1]["key"] = (
        f"lssc/v1/tenant/project/{'6'*32}/{'7'*32}/{version_id}/{second}.lssc"
    )
    with pytest.raises(ArtifactRecordError):
        validate_inventory(inventory, logical)
    inventory["volumes"][1]["key"] = (
        f"lssc/v1/tenant/../{'6'*32}/{artifact_id}/{version_id}/{second}.lssc"
    )
    with pytest.raises(ArtifactRecordError):
        validate_inventory(inventory, logical)


def test_selected_ciphertext_substitution_cannot_release_or_receipt(
    separate_machine_env, tmp_path: Path,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    origin = prepared.volumes[0]
    with LocalFileRangeSource(origin.path) as source:
        approved = approve_container_distribution(
            source, output_dir=origin.path.parent,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    with LocalFileRangeSource(approved.path) as source:
        header = read_metadata(source)
        inspected = inspect_container_metadata(
            source, policy=env.policies["receiver"],
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        exact = io.BytesIO()
        copy_message(source, header,
                     parse_descriptor(inspected.volume_record["messages"][0]["descriptor"]),
                     exact)
    changed = bytearray(exact.getvalue())
    changed[-1] ^= 1
    substituted = io.BytesIO()
    write_container(substituted, recipient_hints=env.policies["receiver"].audience,
                    metadata_ciphertext=header.encrypted_metadata,
                    messages=(bytes(changed),))
    destination = Path(env.policies["receiver"].boundary) / "substituted.bin"
    with pytest.raises(ArtifactError) as error:
        extract_container_message(
            BytesRangeSource(substituted.getvalue()), 0, destination.name,
            authorized=True, policy=env.policies["receiver"],
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert error.value.code is ArtifactErrorCode.INVALID_ARTIFACT
    assert not destination.exists()
    with sqlite3.connect(env.paths["receiver"]) as db:
        assert db.execute("SELECT count(*) FROM consumed WHERE kind='container-selected-v1'").fetchone()[0] == 0


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("failure", ["temporary", "deadline", "cancel"])
def test_selected_operation_shares_limits_through_publication(
    separate_machine_env, tmp_path, monkeypatch, historical, failure,
):
    env = separate_machine_env
    payload = bytes(range(256)) * 512
    origin = _prepare(env, tmp_path, content=payload).volumes[0]
    policy = replace(env.policies["receiver"], controller_fingerprint=None)
    destination = Path(policy.boundary) / "bounded.bin"
    args = dict(
        authorized=True, policy=policy, trust_state_path=env.paths["receiver"],
        gnupg_home=env.homes["receiver"], expected_key=origin.key,
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    with LocalFileRangeSource(origin.path) as source:
        metadata = inspect_container_metadata(source, **{k: v for k, v in args.items() if k != "authorized"})
        if historical:
            extract_container_message(source, 0, "original.bin", **args)
        cipher_length = metadata.volume_record["messages"][0]["descriptor"]["ciphertext_length"]
        limits = ContainerPreparationLimits().file_limits
        if failure == "temporary":
            # Ciphertext plus staged plaintext fits; another output copy does not.
            limits = replace(limits, max_temporary_bytes=cipher_length + 2 * len(payload))
        cancelled = False
        opened = []
        original_open = artifacts_api._open_selected_file
        original_publish = artifacts_api.ExclusiveOutput.publish

        def open_selected(*positional, **keywords):
            result = original_open(*positional, **keywords)
            opened.append(result)
            if failure == "deadline":
                keywords["budget"].deadline = 0
            return result

        def publish(self, name):
            nonlocal cancelled
            result = original_publish(self, name)
            if self.path == destination and failure == "cancel":
                cancelled = True
            return result

        monkeypatch.setattr(artifacts_api, "_open_selected_file", open_selected)
        monkeypatch.setattr(artifacts_api.ExclusiveOutput, "publish", publish)
        operation = open_historical_container_message if historical else extract_container_message
        with pytest.raises(ArtifactError):
            operation(source, 0, destination.name, file_limits=limits,
                      cancel_check=lambda: cancelled, **args)
    assert opened
    assert not destination.exists()
    assert not tuple(Path(policy.boundary).glob(".lssc-*"))
    with sqlite3.connect(env.paths["receiver"]) as db:
        assert db.execute("SELECT count(*) FROM consumed WHERE kind='container-selected-v1'").fetchone()[0] == int(historical)


def test_owner_approval_retries_metadata_length_jitter_and_exhausts_safely(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    origin = _prepare(env, tmp_path).volumes[0]
    successful_dir = tmp_path / "approved-retry"
    exhausted_dir = tmp_path / "approved-exhausted"
    successful_dir.mkdir(mode=0o700)
    exhausted_dir.mkdir(mode=0o700)
    original = artifacts_api._seal
    lengths = []
    def jitter_once(*args, **kwargs):
        value = original(*args, **kwargs)
        if not lengths:
            value += b"test-only-length-jitter" * 8
        lengths.append(len(value))
        return value
    monkeypatch.setattr(artifacts_api, "_seal", jitter_once)
    with LocalFileRangeSource(origin.path) as source:
        approved = approve_container_distribution(
            source, output_dir=successful_dir,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert len(lengths) >= 4
    assert lengths[0] != lengths[1]
    print(f"owner metadata first-pair lengths: {lengths[0]}, {lengths[1]}")
    with LocalFileRangeSource(approved.path) as source:
        observed = inspect_container_metadata(
            source, policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    assert observed.payload_verified is False

    lengths.clear()
    def jitter_every_provisional(*args, **kwargs):
        value = original(*args, **kwargs)
        if len(lengths) % 2 == 0:
            value += b"test-only-length-jitter" * 8
        lengths.append(len(value))
        return value
    monkeypatch.setattr(artifacts_api, "_seal", jitter_every_provisional)
    with LocalFileRangeSource(origin.path) as source:
        with pytest.raises(ArtifactError) as exhausted:
            approve_container_distribution(
                source, output_dir=exhausted_dir,
                policy=env.policies["controller"],
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                limits=ContainerPreparationLimits(max_plan_passes=2),
            )
    assert exhausted.value.code is ArtifactErrorCode.INVALID_ARTIFACT
    assert len(lengths) == 4
    assert lengths[0] != lengths[1] and lengths[2] != lengths[3]
    assert list(exhausted_dir.iterdir()) == []


def test_global_preparation_budget_rejects_without_publication(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(b"small private payload")
    output = tmp_path / "budget-output"
    output.mkdir(mode=0o700)
    with pytest.raises(ArtifactError) as error:
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"],
            trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(max_generated_bytes=1),
        )
    assert error.value.code is ArtifactErrorCode.INVALID_ARTIFACT
    assert list(output.iterdir()) == []
    original_open = Path.open
    attempted_temporary_write = []
    def observed_open(path, *args, **kwargs):
        if str(path).endswith((".metadata-plain", ".plain")):
            attempted_temporary_write.append(path)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observed_open)
    with pytest.raises(ArtifactError):
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"],
            trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(max_temporary_bytes=1),
        )
    assert list(output.iterdir()) == []
    assert attempted_temporary_write == []


def test_preparation_caps_nested_crypto_temporary_space_before_write(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(b"bounded-private" * 300)
    output = tmp_path / "bounded"
    output.mkdir(mode=0o700)
    maximum = 200_000
    actual_limits = []
    original = container_preparation.seal_envelope_file
    def observed_seal(*args, **kwargs):
        actual_limits.append(kwargs["limits"])
        assert 0 < actual_limits[-1].max_temporary_bytes <= maximum
        assert 0 < actual_limits[-1].timeout_seconds <= 60
        assert callable(kwargs["cancel_check"])
        return original(*args, **kwargs)
    monkeypatch.setattr(container_preparation, "seal_envelope_file", observed_seal)
    prepared = prepare_container(
        (ContainerInput(source, "a" * 32),), output_dir=output,
        namespace=ContainerNamespace("tenant", "project", "b" * 32),
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        limits=ContainerPreparationLimits(
            file_limits=replace(ContainerPreparationLimits().file_limits,
                                timeout_seconds=120),
            max_temporary_bytes=maximum, timeout_seconds=60,
        ),
    )
    assert actual_limits
    assert len(prepared.volumes) == 1


def test_preparation_cancellation_during_serialization_leaves_no_volume(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(b"private" * 300)
    output = tmp_path / "cancelled"
    output.mkdir(mode=0o700)
    cancel = [False]
    written = [0]
    original = container_preparation._CheckedWriter.write
    def cancel_after_first_actual_write(writer, data):
        result = original(writer, data)
        if writer.maximum is not None:
            written[0] += result
            cancel[0] = True
        return result
    monkeypatch.setattr(container_preparation._CheckedWriter,
                        "write", cancel_after_first_actual_write)
    with pytest.raises(ArtifactError):
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"], trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(cancel_check=lambda: cancel[0]),
        )
    assert written[0] > 0
    assert list(output.iterdir()) == []


def test_revision_change_after_all_volume_staging_rolls_back_every_output(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 120)
    output = tmp_path / "rollback-volumes"
    output.mkdir(mode=0o700)
    calls = 0
    staged_volumes = 0
    original = artifacts_api._current_authorities
    original_write = container_preparation._write_stream
    def count_volume_writes(destination, *args, **kwargs):
        nonlocal staged_volumes
        if not isinstance(destination, container_preparation._DiscardWriter):
            staged_volumes += 1
        return original_write(destination, *args, **kwargs)
    monkeypatch.setattr(container_preparation, "_write_stream", count_volume_writes)
    def change_at_final_gate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(env.paths["origin"]) as db:
                db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts_api, "_current_authorities", change_at_final_gate)
    with pytest.raises(ArtifactError) as failure:
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"], trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(packing_limits=PackingLimits(
                target_bytes=25_000, tolerance=0.10,
                max_split_iterations=12, max_measure_calls=200,
            )),
        )
    assert failure.value.code is ArtifactErrorCode.AUTHORIZATION_DENIED
    assert calls == 2
    assert staged_volumes >= 2
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("change", ["cancel", "revoke"])
def test_preparation_rolls_back_if_authority_changes_during_publication(
    separate_machine_env, tmp_path, monkeypatch, change,
):
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 120)
    output = tmp_path / "late-publication"
    output.mkdir(mode=0o700)
    cancelled = False
    published = []
    original = container_preparation.ExclusiveOutput.publish

    def publish(self, name):
        nonlocal cancelled
        result = original(self, name)
        if self.path.parent == output and self.path.suffix == ".lssc":
            published.append(result)
            if change == "cancel":
                cancelled = True
            else:
                snapshot = env.snapshots["origin"]
                trust_state.revoke_authority(
                    env.paths["origin"], role="publisher",
                    expected_store_id=snapshot.store_id,
                    expected_revision=snapshot.revision, expected_scope=SCOPE,
                )
        return result

    monkeypatch.setattr(container_preparation.ExclusiveOutput, "publish", publish)
    with pytest.raises(ArtifactError):
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"], trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"], passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(
                packing_limits=PackingLimits(target_bytes=25_000, tolerance=0.10,
                                             max_split_iterations=12, max_measure_calls=200),
                cancel_check=lambda: cancelled,
            ),
        )
    assert len(published) == 1
    assert list(output.iterdir()) == []


def test_discovery_admission_and_cache_recheck_signed_claims_and_current_authority(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    origin = prepared.volumes[0]
    policy = _discovery(env.policies["controller"])
    with LocalFileRangeSource(origin.path) as source:
        observed = inspect_container_metadata(
            source, policy=policy, trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True,
        )
        assert metadata_observation_unchanged(
            source, observed, policy=policy,
            trust_state_path=env.paths["controller"],
        )
        for denied_policy in (
            replace(policy, origin_trust_store_id="f" * 64),
            replace(policy, max_content_lifetime_seconds=1),
            replace(policy, max_content_age_seconds=1),
        ):
            if denied_policy.max_content_age_seconds == 1:
                check_time = observed.logical["created_at"] + 2
            else:
                check_time = observed.logical["created_at"]
            with pytest.raises(ArtifactError):
                inspect_container_metadata(
                    source, policy=denied_policy,
                    trust_state_path=env.paths["controller"],
                    gnupg_home=env.homes["controller"],
                    passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                    allow_origin_unapproved=True, evaluation_time=check_time,
                )
        assert not metadata_observation_unchanged(
            source, observed, policy=replace(policy, max_content_age_seconds=2),
            trust_state_path=env.paths["controller"],
        )
        raw = bytearray(origin.path.read_bytes())
        marker = b"metadata: !!binary |\n  "
        position = raw.find(marker)
        assert position >= 0
        position += len(marker)
        raw[position] = ord("A") if raw[position] != ord("A") else ord("B")
        assert not metadata_observation_unchanged(
            BytesRangeSource(bytes(raw)), observed, policy=policy,
            trust_state_path=env.paths["controller"],
        )
        with pytest.raises(ArtifactError):
            inspect_container_metadata(
                BytesRangeSource(bytes(raw)), policy=policy,
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                allow_origin_unapproved=True,
            )
        with sqlite3.connect(env.paths["controller"]) as db:
            db.execute("INSERT INTO revocations(role,fingerprint,revoked_at) VALUES (?,?,?)",
                       ("publisher", env.publisher, int(__import__("time").time())))
        with pytest.raises(ArtifactError) as revoked:
            metadata_observation_unchanged(
                source, observed, policy=policy,
                trust_state_path=env.paths["controller"],
            )
        assert revoked.value.code is ArtifactErrorCode.AUTHORIZATION_DENIED


def test_explicit_inspection_time_cannot_authorize_expired_acceptance(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    prepared = _prepare(env, tmp_path)
    origin = prepared.volumes[0]
    policy = _discovery(env.policies["controller"])
    signed_time = env.policies["controller"].created_at + 1
    monkeypatch.setattr(artifacts_api, "_clock", lambda: policy.admit_until + 1)
    with LocalFileRangeSource(origin.path) as source:
        inspected = inspect_container_metadata(
            source, policy=policy, trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            allow_origin_unapproved=True, evaluation_time=signed_time,
        )
        assert inspected.payload_verified is False
        with pytest.raises(ArtifactError) as denied:
            accept_container_metadata(
                source, policy=policy, trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            )
        assert denied.value.code is ArtifactErrorCode.AUTHORIZATION_DENIED


def test_live_staged_volumes_share_one_temporary_ceiling(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    small = tmp_path / "small.bin"
    large = tmp_path / "large.bin"
    small.write_bytes(b"small-" * 300)
    large.write_bytes(bytes(range(256)) * 120)
    output = tmp_path / "limited-volumes"
    output.mkdir(mode=0o700)
    packing = PackingLimits(target_bytes=25_000, tolerance=0.10,
                            max_split_iterations=12, max_measure_calls=200)
    staged = []
    original = container_preparation._write_stream

    def limit_after_first_stage(destination, *args, **kwargs):
        if not isinstance(destination, container_preparation._DiscardWriter):
            budget = args[4]
            staged.append(budget.temporary)
            # Constrain this operation's remaining capacity. A second random
            # encryption run cannot establish this operation's live byte count.
            budget.limits = replace(budget.limits, max_temporary_bytes=budget.temporary + 1)
        return original(destination, *args, **kwargs)

    monkeypatch.setattr(container_preparation, "_write_stream", limit_after_first_stage)
    with pytest.raises(ArtifactError):
        prepare_container(
            (ContainerInput(small, "a" * 32), ContainerInput(large, "b" * 32)),
            output_dir=output, namespace=ContainerNamespace("tenant", "project", "c" * 32),
            policy=env.policies["origin"], trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(packing_limits=packing,
                                              max_temporary_bytes=1_000_000),
        )
    assert len(staged) == 1
    assert list(output.iterdir()) == []


def test_generated_allowance_caps_nested_seal_before_crypto(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    source = tmp_path / "source.bin"
    source.write_bytes(b"private payload")
    output = tmp_path / "generated-limit"
    output.mkdir(mode=0o700)
    allowances = []
    original = container_preparation.seal_envelope_file
    def observe_allowance(*args, **kwargs):
        allowances.append(kwargs["limits"].max_envelope_bytes)
        return original(*args, **kwargs)
    monkeypatch.setattr(container_preparation, "seal_envelope_file", observe_allowance)
    with pytest.raises(ArtifactError):
        prepare_container(
            (ContainerInput(source, "a" * 32),), output_dir=output,
            namespace=ContainerNamespace("tenant", "project", "b" * 32),
            policy=env.policies["origin"], trust_state_path=env.paths["origin"],
            gnupg_home=env.homes["origin"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            limits=ContainerPreparationLimits(max_generated_bytes=1),
        )
    assert allowances == [1]
    assert list(output.iterdir()) == []


def test_approval_initial_metadata_open_honors_shared_cancel_and_deadline(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    origin = _prepare(env, tmp_path).volumes[0]
    output = tmp_path / "cancelled-approval"
    output.mkdir(mode=0o700)
    cancelled = [False]
    attempts = []
    original = artifacts_api.open_envelope_file
    def cancel_at_metadata_open(*args, **kwargs):
        attempts.append(kwargs["limits"].timeout_seconds)
        assert 0 < attempts[-1] <= 30
        assert callable(kwargs["cancel_check"])
        cancelled[0] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(artifacts_api, "open_envelope_file", cancel_at_metadata_open)
    with LocalFileRangeSource(origin.path) as source:
        with pytest.raises(ArtifactError):
            approve_container_distribution(
                source, output_dir=output,
                policy=env.policies["controller"],
                trust_state_path=env.paths["controller"],
                gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                limits=ContainerPreparationLimits(timeout_seconds=30,
                    file_limits=replace(ContainerPreparationLimits().file_limits,
                                        timeout_seconds=120),
                    cancel_check=lambda: cancelled[0]),
            )
    assert len(attempts) == 1
    assert list(output.iterdir()) == []
