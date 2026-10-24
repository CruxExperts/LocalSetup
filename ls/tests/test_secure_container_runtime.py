"""Opt-in runtime configuration, exact key recovery, and trust separation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
from threading import Event
import time

import pytest

from ls.core.openpgp import container_runtime as runtime
from ls.core.openpgp import container_identity as identity
from ls.core.openpgp import container_storage as storage
from ls.core.openpgp import generation, trust_state
from ls.core.openpgp.artifacts import ArtifactPolicy
from ls.core.openpgp.container_catalog import CatalogStore
from ls.core.openpgp.container_reconcile import (
    RejectedObservation, ScanCoverage, build_transition,
)


RUNTIME_ID = "a" * 32
MACHINE = identity.MachineBinding("linux-machine-id-v1", "b" * 64)


def _root(tmp_path: Path) -> Path:
    return tmp_path / RUNTIME_ID


def _binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "machine_binding", lambda: MACHINE)


def test_configuration_is_disabled_noncreating_and_compare_and_swap(tmp_path: Path) -> None:
    root = _root(tmp_path)
    initial = runtime.load_runtime_config(root)
    assert initial.revision == 0 and initial.enabled is False and not root.exists()
    configured = runtime.configure_runtime(
        root, expected_revision=0,
        proposed=runtime.RuntimeConfig(RUNTIME_ID, source_ids=("1" * 32,)),
    )
    assert configured.revision == 1
    assert runtime.load_runtime_config(root) == configured
    with pytest.raises(runtime.RuntimeErrorCode, match="revision_conflict"):
        runtime.configure_runtime(root, expected_revision=0, proposed=configured)
    assert not (root / "identity.json").exists()
    assert not (root / "keys").exists()
    assert not (root / "secrets").exists()


def test_concurrent_configuration_has_one_revision_winner(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(RUNTIME_ID))

    def change(index: int) -> str:
        try:
            runtime.configure_runtime(
                root, expected_revision=1,
                proposed=runtime.RuntimeConfig(RUNTIME_ID, source_ids=(str(index) * 32,)),
            )
            return "written"
        except runtime.RuntimeErrorCode as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, (1, 2)))
    assert sorted(outcomes) == ["revision_conflict", "written"]
    assert runtime.load_runtime_config(root).revision == 2


def test_bounded_canonical_configuration_rejects_duplicate_and_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(RUNTIME_ID))
    config_path = root / "runtime.json"
    config_path.write_bytes(b'{"revision":1,"revision":2}')
    config_path.chmod(0o600)
    with pytest.raises(runtime.RuntimeErrorCode, match="state_invalid"):
        runtime.load_runtime_config(root)
    config_path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("{}")
    config_path.symlink_to(outside)
    with pytest.raises(runtime.RuntimeErrorCode, match="state_unavailable"):
        runtime.load_runtime_config(root)


def test_private_root_creation_rejects_symlinked_ancestor_before_write(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    root = tmp_path / "linked" / RUNTIME_ID
    with pytest.raises(runtime.RuntimeErrorCode, match="state_unavailable"):
        runtime.configure_runtime(root, expected_revision=0,
                                  proposed=runtime.RuntimeConfig(RUNTIME_ID))
    assert not (target / RUNTIME_ID).exists()


def test_custom_protected_file_reference_requires_explicit_private_root(tmp_path: Path) -> None:
    from ls.core.openpgp.secrets import SecretProvider, SecretReference

    selected = runtime.KeySelection(
        "A" * 40, b"isolated public fixture", tmp_path / "key-home",
        SecretReference(SecretProvider.PROTECTED_FILE, "FIXTURE_SECRET"),
        protected_file_root=tmp_path / "secrets",
    )
    assert selected.protected_file_root == tmp_path / "secrets"
    with pytest.raises(runtime.RuntimeErrorCode, match="config_invalid"):
        runtime.KeySelection(
            "A" * 40, b"isolated public fixture", tmp_path / "key-home",
            SecretReference(SecretProvider.PROTECTED_FILE, "FIXTURE_SECRET"),
        )


@pytest.mark.parametrize("operation", ["read", "write"])
def test_configuration_rejects_parent_replacement_without_touching_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    root = _root(tmp_path)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(RUNTIME_ID))
    original = (root / "runtime.json").read_bytes()
    held = tmp_path / "held"
    replaced = False

    def replace_root():
        nonlocal replaced
        if not replaced:
            root.rename(held)
            root.mkdir(mode=0o700)
            (root / "sentinel").write_bytes(b"preserve replacement")
            replaced = True

    if operation == "read":
        original_read = storage.os.read
        target = (root / "runtime.json").stat()

        def read_and_replace(descriptor, count):
            data = original_read(descriptor, count)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                replace_root()
            return data

        monkeypatch.setattr(storage.os, "read", read_and_replace)
        action = lambda: runtime.load_runtime_config(root)
    else:
        original_sync = storage.os.fsync

        def sync_and_replace(descriptor):
            original_sync(descriptor)
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                replace_root()

        monkeypatch.setattr(storage.os, "fsync", sync_and_replace)
        action = lambda: runtime.configure_runtime(
            root, expected_revision=1, proposed=runtime.RuntimeConfig(RUNTIME_ID, enabled=True),
        )
    with pytest.raises(runtime.RuntimeErrorCode, match="state_unavailable"):
        action()
    assert replaced
    assert (held / "runtime.json").read_bytes() == original
    assert {path.name for path in root.iterdir()} == {"sentinel"}
    assert (root / "sentinel").read_bytes() == b"preserve replacement"
    assert not list(held.glob(".state-*"))


def test_automatic_setup_requires_stable_binding_without_creating_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    runtime.configure_runtime(root, expected_revision=0, proposed=runtime.RuntimeConfig(RUNTIME_ID))
    monkeypatch.setattr(runtime, "machine_binding", lambda: (_ for _ in ()).throw(
        identity.IdentityError("binding_unavailable")))
    with pytest.raises(runtime.RuntimeErrorCode, match="binding_unavailable"):
        runtime.setup_identity(root)
    assert not (root / "identity.json").exists()
    from ls.core.openpgp.secrets import SecretProvider, SecretReference

    mixed_root = tmp_path / ("e" * 32)
    custom = runtime.KeySelection(
        "A" * 40, b"isolated public fixture", tmp_path / "external-key-home",
        SecretReference(SecretProvider.ENV, "LOCALSETUP_TEST_CUSTOM_PUBLISHER"),
    )
    runtime.configure_runtime(
        mixed_root, expected_revision=0,
        proposed=runtime.RuntimeConfig("e" * 32, custom_publisher=custom),
    )
    with pytest.raises(runtime.RuntimeErrorCode, match="binding_unavailable"):
        runtime.setup_identity(mixed_root)
    assert not (mixed_root / "identity.json").exists()


@pytest.mark.skipif(shutil.which("gpg") is None, reason="GnuPG is unavailable")
def test_real_setup_reuses_keys_recovers_exact_candidate_and_keeps_remote_trust_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generate only in the isolated fixture; never inspect the host keyring."""
    root = _root(tmp_path)
    mixed_root = tmp_path / ("e" * 32)
    _binding(monkeypatch)
    configured = runtime.configure_runtime(
        root, expected_revision=0, proposed=runtime.RuntimeConfig(RUNTIME_ID),
    )
    try:
        first = runtime.setup_identity(root)
        assert first.state == "ready"
        assert first.publisher_fingerprint != first.owner_fingerprint
        assert first.backup_status == "backup_not_configured"
        identity_record = runtime._read(root / "identity.json")
        assert identity_record is not None
        assert identity_record["phase"] == "ready"
        assert identity_record["binding"] == {"provider": MACHINE.provider, "digest": MACHINE.digest}
        assert identity_record["year"] >= 2026
        assert identity_record["publisher"]["reference"]["provider"] == "protected-file"
        assert identity_record["publisher"]["recipe"]["name"].startswith("LocalSetup Encryption ")
        assert "Copyright ©" in identity_record["publisher"]["recipe"]["certification_notations"][0][1]
        assert first.label == identity_record["label"]

        def no_replacement(*_args, **_kwargs):
            raise AssertionError("an existing machine mapping must not generate replacement keys")

        monkeypatch.setattr(runtime, "generate_key", no_replacement)
        monkeypatch.setattr(runtime, "default_key_identity", no_replacement)
        assert runtime.setup_identity(root) == first
        entered = Event()
        release = Event()
        verify_role = runtime._verify_role

        def held_verify(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return verify_role(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "_verify_role", held_verify)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(runtime.setup_identity, root)
                try:
                    assert entered.wait(5)
                    with pytest.raises(runtime.RuntimeErrorCode, match="busy"):
                        runtime.setup_identity(root)
                finally:
                    release.set()
                assert future.result(timeout=30) == first
        # Simulate a lost return after both key homes and local trust became
        # durable, before their fingerprints were recorded in the attempt.
        identity_record["phase"] = "secret_ready"
        identity_record["trust_store_id"] = None
        for role in ("publisher", "owner"):
            identity_record[role]["fingerprint"] = None
            identity_record[role]["certificate"] = None
        runtime._write(root / "identity.json", identity_record)
        recovered = runtime.setup_identity(root)
        assert recovered.publisher_fingerprint == first.publisher_fingerprint
        assert recovered.owner_fingerprint == first.owner_fingerprint

        # An explicit custom selection may reuse caller-owned private stores.
        # The new runtime never generates or copies those keys.
        from ls.core.openpgp.secrets import SecretProvider, SecretReference

        identity_record = runtime._read(root / "identity.json")
        assert identity_record is not None
        def custom_key(role: str) -> runtime.KeySelection:
            item = identity_record[role]
            return runtime.KeySelection(
                item["fingerprint"], base64.b64decode(item["certificate"]),
                Path(item["home"]),
                SecretReference(SecretProvider.PROTECTED_FILE, item["reference"]["name"]),
                protected_file_root=root / "secrets",
            )

        custom_root = tmp_path / ("d" * 32)
        runtime.configure_runtime(
            custom_root, expected_revision=0,
            proposed=runtime.RuntimeConfig(
                "d" * 32, custom_publisher=custom_key("publisher"),
                custom_owner=custom_key("owner"),
            ),
        )
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "machine_binding", lambda: (_ for _ in ()).throw(
                identity.IdentityError("binding_unavailable")))
            custom = runtime.setup_identity(custom_root)
        assert custom.publisher_fingerprint == first.publisher_fingerprint
        assert custom.owner_fingerprint == first.owner_fingerprint
        assert not (custom_root / "keys").exists()
        assert not (custom_root / "secrets").exists()
        runtime.configure_runtime(
            mixed_root, expected_revision=0,
            proposed=runtime.RuntimeConfig("e" * 32, custom_publisher=custom_key("publisher")),
        )
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "generate_key", generation.generate_key)
            patch.setattr(runtime, "default_key_identity", identity.default_key_identity)
            mixed = runtime.setup_identity(mixed_root)
        assert mixed.publisher_fingerprint == first.publisher_fingerprint
        assert mixed.owner_fingerprint not in {first.publisher_fingerprint, first.owner_fingerprint}
        mixed_record = runtime._read(mixed_root / "identity.json")
        assert mixed_record is not None and mixed_record["kind"] == "mixed"
        assert mixed_record["publisher"]["recipe"] is None
        assert mixed_record["owner"]["recipe"] is not None
        publisher_home = Path(identity_record["publisher"]["home"])
        held_home = publisher_home.with_name("publisher-held")
        publisher_home.rename(held_home)
        try:
            with pytest.raises(runtime.RuntimeErrorCode, match="lifecycle_required"):
                runtime.setup_identity(root)
            assert not publisher_home.exists()
        finally:
            held_home.rename(publisher_home)

        configured = runtime.configure_runtime(
            root, expected_revision=configured.revision,
            proposed=runtime.RuntimeConfig(RUNTIME_ID, enabled=True),
        )
        local_contexts = runtime.resolve_runtime_contexts(root)
        assert local_contexts.remote_content is None
        catalog_context = local_contexts.catalog
        assert catalog_context.signer_fingerprint == first.publisher_fingerprint
        assert catalog_context.signer_fingerprint != first.owner_fingerprint
        assert catalog_context.gnupg_home == Path(identity_record["publisher"]["home"])
        assert catalog_context.gnupg_home != Path(identity_record["owner"]["home"])
        boundary = root / "accepted"
        boundary.mkdir(mode=0o700)
        now = int(time.time())
        policy = ArtifactPolicy(
            trust_store_id=catalog_context.trust_store_id,
            trust_revision=catalog_context.trust_revision,
            governance_epoch=catalog_context.governance_epoch,
            scope=catalog_context.expected_scope,
            origin_fingerprint=first.publisher_fingerprint,
            audience=tuple(sorted((first.publisher_fingerprint, first.owner_fingerprint))),
            purpose="catalog.fixture",
            content_revision="fixture:1",
            created_at=now,
            expires_at=now + 3600,
            boundary=boundary,
            controller_fingerprint=first.owner_fingerprint,
        )
        transition = build_transition(
            scan_id="c" * 32, evaluation_time=now,
            source_config_sha256=hashlib.sha256(b"isolated runtime source").hexdigest(),
            policy=policy,
            coverage=(ScanCoverage("private-source", "complete"),),
            observations=(RejectedObservation(
                "private-source", "private document name: runtime sentinel",
                "INVALID_ARTIFACT", now,
            ),),
        )
        store = CatalogStore(root / "catalog", RUNTIME_ID)
        assert store.load(catalog_context).head is None
        committed = store.commit(None, transition, catalog_context)
        assert store.load(catalog_context).head == committed.head

        # Disable after the first shared lease exits. A two-lease resolver
        # would carry the old revision into its later actual-use lease.
        real_runtime_use = runtime.runtime_use
        real_trust_snapshot = runtime._trust_snapshot
        switched = []
        checked = []

        @contextmanager
        def disable_after_first_shared_lease(*args, **kwargs):
            with real_runtime_use(*args, **kwargs) as lease:
                yield lease
            if not kwargs.get("exclusive") and not switched:
                switched.append(True)
                with monkeypatch.context() as patch:
                    patch.setattr(runtime, "runtime_use", real_runtime_use)
                    runtime.configure_runtime(
                        root, expected_revision=configured.revision,
                        proposed=runtime.RuntimeConfig(RUNTIME_ID, enabled=False),
                    )

        def check_actual_use(*args, **kwargs):
            current = runtime._load_config_locked(root)
            assert current.enabled and current.revision == configured.revision
            checked.append(True)
            return real_trust_snapshot(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "runtime_use", disable_after_first_shared_lease)
            patch.setattr(runtime, "_trust_snapshot", check_actual_use)
            resolved_before_disable = runtime.resolve_runtime_contexts(root)
        assert switched and checked
        assert resolved_before_disable.catalog.signer_fingerprint == first.publisher_fingerprint
        assert runtime.load_runtime_config(root).enabled is False
        configured = runtime.configure_runtime(
            root, expected_revision=configured.revision + 1,
            proposed=runtime.RuntimeConfig(RUNTIME_ID, enabled=True),
        )
        identity_record = runtime._read(root / "identity.json")
        assert identity_record is not None
        remote_path = root / "remote-content.sqlite3"
        remote = trust_state.initialize_trust_state(
            remote_path, scope=("remote-content",),
            publisher_certificate=base64.b64decode(identity_record["publisher"]["certificate"]),
            owner_certificate=base64.b64decode(identity_record["owner"]["certificate"]),
        )
        runtime.configure_runtime(
            root, expected_revision=configured.revision,
            proposed=runtime.RuntimeConfig(
                RUNTIME_ID, enabled=True,
                remote_content=runtime.RemoteContentTrust(remote_path, remote.store_id, remote.scope),
            ),
        )
        contexts = runtime.resolve_runtime_contexts(root)
        assert contexts.remote_content.store_id == remote.store_id
        assert contexts.catalog.trust_store_id != remote.store_id
        assert contexts.catalog.trust_state_path == root / "local-trust.sqlite3"
        assert contexts.catalog.signer_fingerprint == first.publisher_fingerprint
        assert contexts.catalog.recipient_fingerprints == (first.publisher_fingerprint,)

        with monkeypatch.context() as patch:
            patch.setattr(trust_state, "load_trust_state", no_replacement)
            assert runtime.inspect_runtime_status(root).state == "recorded_ready"
        complete = runtime._read(root / "identity.json")
        assert complete is not None
        incomplete = json.loads(json.dumps(complete))
        incomplete["publisher"]["fingerprint"] = None
        incomplete["publisher"]["certificate"] = None
        runtime._write(root / "identity.json", incomplete)
        with pytest.raises(runtime.RuntimeErrorCode, match="state_invalid"):
            runtime.setup_identity(root)
        runtime._write(root / "identity.json", complete)
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "machine_binding", lambda: identity.MachineBinding(
                "linux-machine-id-v1", "c" * 64))
            with pytest.raises(runtime.RuntimeErrorCode, match="foreign_binding"):
                runtime.setup_identity(root)
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "inspect_generation_candidate", no_replacement)
            with pytest.raises(runtime.RuntimeErrorCode, match="lifecycle_required"):
                runtime.setup_identity(root)
    finally:
        mixed_record = runtime._read(mixed_root / "identity.json")
        if mixed_record is not None:
            mixed_home = Path(mixed_record["owner"]["home"])
            if mixed_home.is_relative_to(mixed_root / "keys") and mixed_home.exists():
                generation._stop_gpg_agent(mixed_home)
        identity_record = runtime._read(root / "identity.json")
        if identity_record is not None:
            for role in ("publisher", "owner"):
                home = Path(identity_record[role]["home"])
                if home.is_relative_to(root / "keys") and home.exists():
                    generation._stop_gpg_agent(home)
