from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ls.core.openpgp import publishing_transition, transition, trust_state
from ls.core.openpgp.artifact_records import ArtifactProvenance, canonical_bytes
from ls.core.openpgp.artifacts import (
    ArtifactPolicy, ArtifactErrorCode, ContainerInput, ContainerNamespace,
    prepare_container, approve_container_distribution,
)
from ls.core.openpgp import artifacts as artifacts_api
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.core.openpgp.container_catalog import (
    CatalogContext, CatalogError, CatalogErrorCode, CatalogLimits, CatalogStore,
)
from ls.core.openpgp.container_reconcile import (
    CatalogObservation, ContainerCandidate, RejectedObservation, ScanCoverage,
    build_transition, inspect_candidate, metadata_cache_for, ReconcileError,
)
from ls.core.openpgp.container_versions import VersionNode, build_version_graph, version_heads
from ls.core.openpgp.envelope import seal_envelope
from ls.core.openpgp.secrets import SecretProvider, SecretReference, SecretResolver
from ls.core.agent.runtime_lock import runtime_use
from ls.tests.test_openpgp_artifacts import (
    _create_key, _gpg, _prepare, _private_fingerprints,
    separate_machine_env, SECRET_REFERENCE as B2_SECRET_REFERENCE, PASSPHRASE,
)


SCOPE = ("agentq:catalog-fixture",)
SECRET_NAME = "LS_SECURE_CONTAINER_CATALOG_TEST_SECRET"
SECRET_REFERENCE = SecretReference(SecretProvider.ENV, SECRET_NAME)
CATALOG_ID = "a" * 32


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory):
    if shutil.which("gpg") is None:
        pytest.skip("GnuPG is required for disposable catalog encryption keys")
    home = tmp_path_factory.mktemp("secure-catalog-gpg")
    home.chmod(0o700)
    owner, owner_certificate = _create_key(home, "Catalog Owner")
    publisher, publisher_certificate = _create_key(home, "Catalog Publisher")
    fixture = SimpleNamespace(
        home=home, owner=owner, owner_certificate=owner_certificate,
        publisher=publisher, publisher_certificate=publisher_certificate,
    )
    try:
        yield fixture
    finally:
        subprocess.run(
            ("gpgconf", "--homedir", str(home), "--kill", "gpg-agent"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, check=True,
        )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keys):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    boundary = private / "accepted"
    boundary.mkdir(mode=0o700)
    trust_path = private / "trust.db"
    fingerprints = {
        keys.owner_certificate: keys.owner,
        keys.publisher_certificate: keys.publisher,
    }
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(
            primary_fingerprint=fingerprints[certificate]
        ),
    )
    monkeypatch.setattr(transition, "_validate_owner_usable", lambda *_, **__: None)
    monkeypatch.setattr(publishing_transition, "_validate_party", lambda *_, **__: None)
    snapshot = trust_state.initialize_trust_state(
        trust_path, scope=SCOPE, owner_certificate=keys.owner_certificate,
        publisher_certificate=keys.publisher_certificate,
    )
    resolver = SecretResolver(env_values={SECRET_NAME: PASSPHRASE})
    now = int(__import__("time").time())
    policy = ArtifactPolicy(
        trust_store_id=snapshot.store_id,
        trust_revision=snapshot.revision,
        governance_epoch=snapshot.epoch,
        scope=SCOPE,
        origin_fingerprint=keys.publisher,
        audience=tuple(sorted((keys.owner, keys.publisher))),
        purpose="catalog.fixture",
        content_revision="fixture:1",
        created_at=now,
        expires_at=now + 3600,
        boundary=boundary,
        controller_fingerprint=keys.owner,
    )
    context = CatalogContext(
        catalog_id=CATALOG_ID,
        trust_state_path=trust_path,
        trust_store_id=snapshot.store_id,
        expected_scope=SCOPE,
        role="publisher",
        trust_revision=snapshot.revision,
        governance_epoch=snapshot.epoch,
        signer_fingerprint=keys.publisher,
        recipient_fingerprints=policy.audience,
        gnupg_home=keys.home,
        passphrase_reference=SECRET_REFERENCE,
        secret_resolver=resolver,
    )
    return SimpleNamespace(
        private=private, trust_path=trust_path, snapshot=snapshot,
        policy=policy, context=context,
        store=CatalogStore(private / "catalog", CATALOG_ID), keys=keys,
    )


@pytest.fixture
def three_home_catalog_env(separate_machine_env):
    try:
        yield separate_machine_env
    finally:
        for home in separate_machine_env.homes.values():
            subprocess.run(
                ("gpgconf", "--homedir", str(home), "--kill", "gpg-agent"),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15, check=True,
            )


def _transition(policy: ArtifactPolicy, *, scan_id: str = "b" * 32,
                evaluation_time: int | None = None):
    if evaluation_time is None:
        evaluation_time = int(__import__("time").time())
    return build_transition(
        scan_id=scan_id,
        evaluation_time=evaluation_time,
        source_config_sha256=hashlib.sha256(b"fixture source configuration").hexdigest(),
        policy=policy,
        coverage=(ScanCoverage("private-source", "complete"),),
        observations=(RejectedObservation(
            "private-source", "private document name: sentinel-catalog-locator",
            "INVALID_ARTIFACT", evaluation_time,
        ),),
    )


def test_catalog_encrypts_private_state_and_does_not_rewrite_unchanged_inputs(catalog_env) -> None:
    store = catalog_env.store
    context = catalog_env.context
    initial = store.load(context)
    assert initial.head is None

    transition_value = _transition(catalog_env.policy)
    committed = store.commit(None, transition_value, context)
    assert committed.head is not None
    assert committed.sequence == 1
    rejected = committed.catalog["reconciliation_history"][0]["rejected"][0]
    assert rejected["locator"] == "private document name: sentinel-catalog-locator"
    assert rejected["status"] == "rejected"
    assert committed.catalog["sources"][0]["coverage_status"] == "complete"

    head = json.loads((store.root / "HEAD").read_text(encoding="utf-8"))
    assert set(head) == {"schema", "major", "cipher_sha256"}
    assert head["cipher_sha256"] == committed.head
    object_files = tuple((store.objects).iterdir())
    assert len(object_files) == 1
    assert object_files[0].name == f"{committed.head}.blob"
    assert object_files[0].stat().st_mode & 0o777 == 0o600
    for path in (store.root / "HEAD", *object_files, store.root / ".runtime-use.lock"):
        if path.exists():
            assert b"sentinel-catalog-locator" not in path.read_bytes()
            assert b"private document name" not in path.read_bytes()

    loaded = store.load(context)
    assert loaded == committed
    repeated = store.commit(committed.head, transition_value, context)
    assert repeated == committed
    assert tuple(store.objects.iterdir()) == object_files


def test_catalog_commit_uses_compare_and_swap_and_rejects_context_mismatch(catalog_env) -> None:
    store = catalog_env.store
    context = catalog_env.context
    old = store.load(context)
    committed = store.commit(None, _transition(catalog_env.policy), context)

    with pytest.raises(CatalogError) as raised:
        store.commit(old.head, _transition(catalog_env.policy, scan_id="c" * 32), context)
    assert raised.value.code is CatalogErrorCode.CONFLICT
    assert raised.value.current_head == committed.head

    wrong_context = replace(
        context,
        recipient_fingerprints=(context.signer_fingerprint,),
    )
    with pytest.raises(CatalogError):
        store.load(wrong_context)
    assert store.load(context).head == committed.head


def test_receipted_candidate_can_recover_after_head_write_failure(catalog_env, monkeypatch) -> None:
    store = catalog_env.store
    context = catalog_env.context
    transition_value = _transition(catalog_env.policy)
    original_writer = store._write_head

    def fail_before_head(_digest: str) -> None:
        raise OSError("simulated interruption before HEAD replacement")

    monkeypatch.setattr(store, "_write_head", fail_before_head)
    with pytest.raises(CatalogError) as raised:
        store.commit(None, transition_value, context)
    candidate = raised.value.candidate_digest
    assert raised.value.code is CatalogErrorCode.UNAVAILABLE
    assert candidate is not None
    assert store.load(context).head is None
    assert (store.objects / f"{candidate}.blob").exists()

    monkeypatch.setattr(store, "_write_head", original_writer)
    recovered = store.recover_candidate(candidate, expected_head=None, context=context)
    assert recovered.head == candidate
    assert recovered.sequence == 1


def test_unreceipted_candidate_cannot_advance_head(catalog_env) -> None:
    store = catalog_env.store
    context = catalog_env.context
    store._ensure_directories()
    plaintext = canonical_bytes({"untrusted": "not receipted"})
    ciphertext = seal_envelope(
        plaintext,
        gnupg_home=context.gnupg_home,
        signer_fingerprint=context.signer_fingerprint,
        recipient_fingerprints=context.recipient_fingerprints,
        local_trust=__import__("ls.core.openpgp.contracts", fromlist=["LocalTrust"]).LocalTrust(
            {context.signer_fingerprint}
        ),
        passphrase_reference=context.passphrase_reference,
        secret_resolver=context.secret_resolver,
    )
    candidate = store._write_object(ciphertext)

    with pytest.raises(CatalogError) as raised:
        store.recover_candidate(candidate, expected_head=None, context=context)
    assert raised.value.code is CatalogErrorCode.AUTHORIZATION_DENIED
    assert store.load(context).head is None


def test_catalog_migration_keeps_old_ciphertext_and_is_explicitly_recoverable(
    catalog_env, monkeypatch,
) -> None:
    store = catalog_env.store
    context = catalog_env.context
    initial = store.commit(None, _transition(catalog_env.policy), context)
    old_ciphertext = (store.objects / f"{initial.head}.blob").read_bytes()
    original_writer = store._write_head
    monkeypatch.setattr(
        store, "_write_head",
        lambda _digest: (_ for _ in ()).throw(OSError("simulated migration interruption")),
    )

    with pytest.raises(CatalogError) as raised:
        store.migrate(
            initial.head, old_context=context, new_context=context,
            transform=lambda catalog: catalog,
        )
    candidate = raised.value.candidate_digest
    assert candidate is not None
    assert store.load(context).head == initial.head
    assert (store.objects / f"{initial.head}.blob").read_bytes() == old_ciphertext

    monkeypatch.setattr(store, "_write_head", original_writer)
    migrated = store.recover_candidate(
        candidate, expected_head=initial.head, context=context,
    )
    assert migrated.head == candidate
    assert migrated.sequence == initial.sequence + 1
    assert (store.objects / f"{initial.head}.blob").exists()


def test_trust_anchor_detects_head_only_replay_after_two_commits(catalog_env) -> None:
    store = catalog_env.store
    context = catalog_env.context
    first = store.commit(None, _transition(catalog_env.policy), context)
    second = store.commit(first.head, _transition(catalog_env.policy, scan_id="c" * 32),
                          context)
    assert second.sequence == 2
    store._write_head(first.head)
    with pytest.raises(CatalogError) as replay:
        store.load(context)
    assert replay.value.code is CatalogErrorCode.CORRUPT
    store._write_head(second.head)
    assert store.load(context).head == second.head


def test_interrupted_head_switch_requires_explicit_receipted_recovery(
    catalog_env, monkeypatch,
) -> None:
    store = catalog_env.store
    context = catalog_env.context
    original_anchor = store._write_anchor
    def interrupt_commit_anchor(context_value, phase, sequence, previous, digest):
        if phase == "committed":
            raise OSError("simulated interruption after HEAD switch")
        return original_anchor(context_value, phase, sequence, previous, digest)
    monkeypatch.setattr(store, "_write_anchor", interrupt_commit_anchor)
    with pytest.raises(CatalogError) as interrupted:
        store.commit(None, _transition(catalog_env.policy), context)
    candidate = interrupted.value.candidate_digest
    assert candidate is not None
    assert store._read_head() == candidate
    with pytest.raises(CatalogError) as pending:
        store.load(context)
    assert pending.value.code is CatalogErrorCode.UNAVAILABLE
    monkeypatch.setattr(store, "_write_anchor", original_anchor)
    recovered = store.recover_candidate(candidate, expected_head=None, context=context)
    assert recovered.head == candidate
    assert recovered.sequence == 1


@pytest.mark.parametrize("target", ["head", "object"])
def test_catalog_rejects_fifo_without_blocking(catalog_env, target: str) -> None:
    store = catalog_env.store
    store._ensure_directories()
    digest = hashlib.sha256(b"opaque").hexdigest()
    if target == "head":
        os.mkfifo(store.root / "HEAD", 0o600)
    else:
        (store.root / "HEAD").write_bytes(canonical_bytes({
            "schema": "localsetup.secure-container.catalog-head",
            "major": 1,
            "cipher_sha256": digest,
        }))
        (store.root / "HEAD").chmod(0o600)
        os.mkfifo(store.objects / f"{digest}.blob", 0o600)

    with pytest.raises(CatalogError) as raised:
        if target == "object":
            store._read_object(digest)
        else:
            store.load(catalog_env.context)
    assert raised.value.code is CatalogErrorCode.CORRUPT


@pytest.mark.parametrize("unsafe", ["symlink", "writable"])
def test_catalog_rejects_unsafe_ancestry_before_creating_state(catalog_env, unsafe: str) -> None:
    env = catalog_env
    outside = env.private / "outside"
    outside.mkdir(mode=0o700)
    if unsafe == "symlink":
        selected = env.private / "alias"
        selected.symlink_to(outside, target_is_directory=True)
    else:
        selected = outside
        outside.chmod(0o777)
    try:
        store = CatalogStore(selected / "new" / "catalog", CATALOG_ID)
        with pytest.raises(CatalogError) as error:
            store.load(env.context)
        assert error.value.code is CatalogErrorCode.UNAVAILABLE
        assert not list(outside.iterdir())
    finally:
        outside.chmod(0o700)


def test_catalog_lock_fails_busy_without_waiting(catalog_env) -> None:
    store = catalog_env.store
    store._ensure_directories()
    with runtime_use(store.root, exclusive=True, timeout=0, create=True):
        with pytest.raises(CatalogError) as raised:
            store.load(catalog_env.context)
    assert raised.value.code is CatalogErrorCode.BUSY


def test_real_b2_approved_metadata_catalog_cache_requires_current_receipts(
    catalog_env, monkeypatch,
) -> None:
    env = catalog_env
    source_path = env.private / "source.bin"
    source_path.write_bytes(b"private catalog document")
    volumes = env.private / "volumes"
    volumes.mkdir(mode=0o700)
    prepared = prepare_container(
        (ContainerInput(source_path, "e" * 32),), output_dir=volumes,
        namespace=ContainerNamespace("tenant", "project", "f" * 32),
        policy=env.policy, trust_state_path=env.trust_path,
        gnupg_home=env.keys.home, passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.context.secret_resolver,
    )
    with LocalFileRangeSource(prepared.volumes[0].path) as source:
        approved = approve_container_distribution(
            source, output_dir=volumes, policy=env.policy,
            trust_state_path=env.trust_path, gnupg_home=env.keys.home,
            passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver,
        )
    now = int(__import__("time").time())
    with LocalFileRangeSource(approved.path) as source:
        candidate = ContainerCandidate("local", "local:opaque", source,
                                       expected_key=approved.key)
        observed = inspect_candidate(
            candidate, policy=env.policy, trust_state_path=str(env.trust_path),
            gnupg_home=str(env.keys.home), passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver, evaluation_time=now,
        )
        assert isinstance(observed, CatalogObservation)
        assert observed.metadata.origin_volume_id == prepared.volumes[0].volume_id
        assert observed.metadata.volume_id == approved.volume_id
        transition_value = build_transition(
            scan_id="d" * 32, evaluation_time=now,
            source_config_sha256=hashlib.sha256(b"local-source").hexdigest(),
            policy=env.policy, coverage=(ScanCoverage("local", "complete"),),
            observations=(observed,),
        )
        committed = env.store.commit(None, transition_value, env.context)
        loaded = env.store.load(env.context)
        cache = metadata_cache_for(loaded, observed.identity)
        assert cache is not None and committed.head == loaded.head
        assert replace(loaded) == loaded
        changed_catalog = copy.deepcopy(loaded.catalog)
        changed_claims = changed_catalog["containers"][0]["authenticated_metadata"]
        changed_claims["logical"]["files"][0]["provenance"]["original_name"] = "forged.bin"
        changed_claims["logical_commitment_sha256"] = hashlib.sha256(
            canonical_bytes(changed_claims["logical"])
        ).hexdigest()
        for change in ({"catalog": changed_catalog}, {"head": "0" * 64},
                       {"sequence": loaded.sequence + 1},
                       {"_proof": hashlib.sha256(b"forged").hexdigest()}):
            with pytest.raises(CatalogError) as copied:
                replace(loaded, **change)
            assert copied.value.code is CatalogErrorCode.AUTHORIZATION_DENIED
        row = loaded.catalog["containers"][0]
        assert row["origin_volume_id"] == prepared.volumes[0].volume_id
        assert row["volume_id"] == approved.volume_id
        assert row["file_inventory"][0]["file_id"] == "e" * 32
        with pytest.raises(ReconcileError):
            metadata_cache_for(SimpleNamespace(catalog=loaded.catalog, head=loaded.head),
                               observed.identity)

        original_inspect = artifacts_api.inspect_container_metadata
        monkeypatch.setattr(artifacts_api, "inspect_container_metadata",
                            lambda *args, **kwargs: pytest.fail("cache decrypted metadata"))
        reused = inspect_candidate(
            candidate, policy=env.policy, trust_state_path=str(env.trust_path),
            gnupg_home=str(env.keys.home), passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver, evaluation_time=now,
            cached=cache,
        )
        assert isinstance(reused, CatalogObservation) and reused.cache_reused
        wrong = ContainerCandidate("local", "local:wrong-key", source,
                                   expected_key=f"{approved.key}.wrong")
        denied_key = inspect_candidate(
            wrong, policy=env.policy, trust_state_path=str(env.trust_path),
            gnupg_home=str(env.keys.home), passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver, evaluation_time=now,
            cached=cache,
        )
        assert isinstance(denied_key, RejectedObservation)
        assert denied_key.error_code == ArtifactErrorCode.AUTHORIZATION_DENIED.value
        monkeypatch.setattr(artifacts_api, "_clock", lambda: env.policy.expires_at + 1)
        expired = inspect_candidate(
            candidate, policy=env.policy, trust_state_path=str(env.trust_path),
            gnupg_home=str(env.keys.home), passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver, evaluation_time=now,
            cached=cache,
        )
        assert isinstance(expired, RejectedObservation)
        assert expired.error_code == ArtifactErrorCode.AUTHORIZATION_DENIED.value
        monkeypatch.setattr(artifacts_api, "_clock", lambda: now)
        with __import__("sqlite3").connect(env.trust_path) as db:
            db.execute("INSERT INTO revocations(role,fingerprint,revoked_at) VALUES (?,?,?)",
                       ("publisher", env.keys.publisher, now))
        revoked = inspect_candidate(
            candidate, policy=env.policy, trust_state_path=str(env.trust_path),
            gnupg_home=str(env.keys.home), passphrase_reference=SECRET_REFERENCE,
            secret_resolver=env.context.secret_resolver, evaluation_time=now,
            cached=cache,
        )
        assert isinstance(revoked, RejectedObservation)
        assert revoked.error_code == ArtifactErrorCode.AUTHORIZATION_DENIED.value
        monkeypatch.setattr(artifacts_api, "inspect_container_metadata", original_inspect)
        authenticated = loaded.catalog["containers"][0]["authenticated_metadata"]
        authenticated["logical"]["files"][0]["provenance"]["original_name"] = "mutated.bin"
        authenticated["logical_commitment_sha256"] = hashlib.sha256(
            canonical_bytes(authenticated["logical"])
        ).hexdigest()
        with pytest.raises(ReconcileError) as mutated_snapshot:
            metadata_cache_for(loaded, observed.identity)
        assert mutated_snapshot.value.code == "AUTHORIZATION_DENIED"


def test_three_home_recipient_catalog_uses_independent_local_trust_instance(
    three_home_catalog_env, tmp_path: Path, monkeypatch,
) -> None:
    env = three_home_catalog_env
    base = _prepare(env, tmp_path, content=b"recipient catalog base")
    base_origin = base.volumes[0]
    child_source = tmp_path / "child-source.bin"
    child_source.write_bytes(b"recipient catalog child")
    child = prepare_container(
        (ContainerInput(child_source, "c" * 32,
                        ArtifactProvenance(original_name="private-child.bin")),),
        output_dir=base_origin.path.parent,
        namespace=ContainerNamespace("test-tenant", "test-project", "a" * 32),
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"],
        passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
        artifact_id=base.artifact_id, parent_versions=(base.version_id,),
    )
    assert child.artifact_id == base.artifact_id
    assert child.version_id != base.version_id
    child_origin = child.volumes[0]
    approved_volumes = []
    for origin in (base_origin, child_origin):
        with LocalFileRangeSource(origin.path) as source:
            approved_volumes.append(approve_container_distribution(
            source, output_dir=origin.path.parent,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
            ))
    local_owner, owner_cert = _create_key(env.homes["controller"], "Local Catalog Owner")
    _gpg(env.homes["receiver"], "--import", input_data=owner_cert)
    recipient_cert = _gpg(env.homes["receiver"], "--export", env.recipient)
    local_certificates = {owner_cert: local_owner, recipient_cert: env.recipient}
    monkeypatch.setattr(
        trust_state, "inspect_key",
        lambda *, certificate, **_: SimpleNamespace(
            primary_fingerprint=local_certificates[certificate]
        ),
    )
    local_scope = ("local:catalog-fixture",)
    local_trust_path = env.paths["receiver"].parent / "catalog-trust.db"
    local_snapshot = trust_state.initialize_trust_state(
        local_trust_path, scope=local_scope,
        owner_certificate=owner_cert, publisher_certificate=recipient_cert,
    )
    assert local_snapshot.store_id != env.snapshots["receiver"].store_id
    assert _private_fingerprints(env.homes["receiver"]) == {env.recipient}
    local_context = CatalogContext(
        catalog_id=CATALOG_ID, trust_state_path=local_trust_path,
        trust_store_id=local_snapshot.store_id, expected_scope=local_scope,
        role="publisher", trust_revision=local_snapshot.revision,
        governance_epoch=local_snapshot.epoch,
        signer_fingerprint=env.recipient,
        recipient_fingerprints=(env.recipient,),
        gnupg_home=env.homes["receiver"],
        passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    store = CatalogStore(env.paths["receiver"].parent / "local-catalog", CATALOG_ID)
    now = int(__import__("time").time())
    with LocalFileRangeSource(approved_volumes[0].path) as base_source, \
            LocalFileRangeSource(approved_volumes[1].path) as child_source:
        candidates = (
            ContainerCandidate("carrier", "carrier:base", base_source,
                               expected_key=approved_volumes[0].key),
            ContainerCandidate("carrier", "carrier:child", child_source,
                               expected_key=approved_volumes[1].key),
        )
        observed_rows = tuple(inspect_candidate(
            candidate, policy=env.policies["receiver"],
            trust_state_path=str(env.paths["receiver"]),
            gnupg_home=str(env.homes["receiver"]),
            passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
            evaluation_time=now,
        ) for candidate in candidates)
        assert all(isinstance(row, CatalogObservation) for row in observed_rows)
        transition_value = build_transition(
            scan_id="c" * 32, evaluation_time=now,
            source_config_sha256=hashlib.sha256(b"carrier-config").hexdigest(),
            policy=env.policies["receiver"],
            coverage=(ScanCoverage("carrier", "complete"),),
            observations=observed_rows,
        )
        committed = store.commit(None, transition_value, local_context)
        loaded = store.load(local_context)
        assert loaded.head == committed.head
        rows = {row["version_id"]: row for row in loaded.catalog["containers"]}
        assert set(rows) == {base.version_id, child.version_id}
        assert rows[base.version_id]["parents"] == []
        assert rows[base.version_id]["volume_id"] == approved_volumes[0].volume_id
        assert rows[base.version_id]["origin_volume_id"] == base_origin.volume_id
        assert rows[base.version_id]["file_inventory"][0]["file_id"] == "b" * 32
        assert rows[child.version_id]["parents"] == [base.version_id]
        assert rows[child.version_id]["file_inventory"][0]["file_id"] == "c" * 32
        assert rows[child.version_id]["volume_id"] == approved_volumes[1].volume_id
        assert rows[child.version_id]["origin_volume_id"] == child_origin.volume_id
        graph = build_version_graph(VersionNode(
            row["artifact_id"], row["version_id"], tuple(row["parents"]),
            row["commitment_sha256"],
        ) for row in loaded.catalog["versions"] if row.get("status") != "collision")
        assert version_heads(graph, artifact_id=base.artifact_id) == (child.version_id,)
        cache = metadata_cache_for(loaded, observed_rows[1].identity)
        assert cache is not None
        original_inspect = artifacts_api.inspect_container_metadata
        monkeypatch.setattr(artifacts_api, "inspect_container_metadata",
                            lambda *args, **kwargs: pytest.fail("unchanged cache decrypted"))
        reused = inspect_candidate(
            candidates[1], policy=env.policies["receiver"],
            trust_state_path=str(env.paths["receiver"]),
            gnupg_home=str(env.homes["receiver"]),
            passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
            evaluation_time=now, cached=cache,
        )
        assert isinstance(reused, CatalogObservation) and reused.cache_reused
        monkeypatch.setattr(artifacts_api, "inspect_container_metadata", original_inspect)
        with pytest.raises(CatalogError) as swapped_catalog:
            store.load(replace(local_context, trust_state_path=env.paths["receiver"]))
        assert swapped_catalog.value.code is CatalogErrorCode.AUTHORIZATION_DENIED
        swapped_content = inspect_candidate(
            candidates[1], policy=env.policies["receiver"],
            trust_state_path=str(local_trust_path),
            gnupg_home=str(env.homes["receiver"]),
            passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
            evaluation_time=now,
        )
        assert isinstance(swapped_content, RejectedObservation)
        assert swapped_content.error_code == ArtifactErrorCode.AUTHORIZATION_DENIED.value

        revoked = trust_state.revoke_authority(
            local_trust_path, role="publisher", expected_scope=local_scope,
            expected_store_id=local_snapshot.store_id,
            expected_revision=local_snapshot.revision,
        )
        assert revoked.publisher is None
        assert store.load(local_context).head == committed.head
        next_transition = build_transition(
            scan_id="d" * 32, evaluation_time=now,
            source_config_sha256=hashlib.sha256(b"carrier-config").hexdigest(),
            policy=env.policies["receiver"],
            coverage=(ScanCoverage("carrier", "complete"),),
            observations=observed_rows,
        )
        with pytest.raises(CatalogError) as denied_commit:
            store.commit(committed.head, next_transition, local_context)
        assert denied_commit.value.code is CatalogErrorCode.AUTHORIZATION_DENIED
        assert store.load(local_context).head == committed.head
        remote_still_valid = inspect_candidate(
            candidates[1], policy=env.policies["receiver"],
            trust_state_path=str(env.paths["receiver"]),
            gnupg_home=str(env.homes["receiver"]),
            passphrase_reference=B2_SECRET_REFERENCE, secret_resolver=env.resolver,
            evaluation_time=now, cached=cache,
        )
        assert isinstance(remote_still_valid, CatalogObservation)
        assert remote_still_valid.cache_reused
