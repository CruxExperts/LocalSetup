"""Ciphertext-only restart, current authority, and immutable publication."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from ls.core.openpgp import container_runtime as runtime
from ls.core.openpgp import container_transfer as transfer
from ls.core.openpgp.artifacts import (
    ArtifactError, accept_container_metadata, approve_container_distribution,
)
from ls.core.openpgp.container_catalog import CatalogContext
from ls.core.openpgp.container_format import read_metadata
from ls.core.openpgp.container_sources import BytesRangeSource, LocalFileRangeSource
from ls.tests.test_openpgp_artifacts import (
    SECRET_REFERENCE, SCOPE, _prepare, separate_machine_env,
)


def test_transfer_limits_and_early_cancel_do_not_create_cache(tmp_path: Path) -> None:
    with pytest.raises(transfer.TransferError, match="limits_invalid"):
        transfer.TransferLimits(chunk_bytes=1)
    root = tmp_path / "cache"
    with pytest.raises(transfer.TransferError, match="local_context_invalid"):
        transfer.resume_selected_ciphertext(
            object(), 0, cache_root=root, local_context=object(),
            policy=object(), trust_state_path=tmp_path, gnupg_home=tmp_path,
            cancel_check="not callable",
        )
    assert not root.exists()


def test_metadata_timeout_applies_without_a_cancel_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])

    def delayed_inspection(*_args, **_kwargs):
        clock[0] = 2.0
        return object()

    monkeypatch.setattr(transfer, "inspect_container_metadata", delayed_inspection)
    with pytest.raises(transfer.TransferError, match="deadline_exceeded"):
        transfer.inspect_transfer_metadata(
            object(), policy=object(), trust_state_path="fixture", gnupg_home="fixture",
            timeout_seconds=1,
        )


def test_provider_never_receives_unverified_replacement_bytes(tmp_path: Path) -> None:
    source = tmp_path / "changed.cipher"
    source.write_bytes(b"changed")
    source.chmod(0o600)

    class Provider:
        calls = 0

        def create_if_absent(self, *_args, **_kwargs):
            self.calls += 1
            return "created"

        def observe(self, _key):
            self.calls += 1
            return None

    provider = Provider()
    with pytest.raises(transfer.TransferError, match="ciphertext_invalid"):
        transfer._publish_file_exact(
            source, destination=provider, key="opaque", length=7,
            sha256=hashlib.sha256(b"correct").hexdigest(), refresh=lambda: None,
        )
    assert provider.calls == 0


def test_progress_crypto_uses_transfer_deadline_before_record_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transfer, "_local_authority", lambda _context: None)
    budget = transfer._Budget(transfer.ContainerPreparationLimits(timeout_seconds=1))
    context = SimpleNamespace(signer_fingerprint="A" * 40, gnupg_home=tmp_path,
                              passphrase_reference=None, secret_resolver=None)

    def delayed_seal(_source, output, **kwargs):
        assert kwargs["limits"].timeout_seconds <= 1
        output.write_bytes(b"fixture cipher")
        clock[0] = 2.0

    monkeypatch.setattr(transfer, "seal_envelope_file", delayed_seal)
    with pytest.raises(transfer.ContainerPreparationError, match="deadline"):
        transfer._write_progress(tmp_path / "progress.json", {}, context, budget)
    assert not list(tmp_path.iterdir()) and budget.temporary == 0


@pytest.mark.parametrize("failure", ["deadline", "cancel"])
def test_provider_late_failure_preserves_uncertain_result_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    source = tmp_path / "volume.lssc"
    data = b"verified opaque ciphertext"
    source.write_bytes(data)
    source.chmod(0o600)
    clock, cancelled = [0.0], [False]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])

    class Provider:
        calls, observations = 0, 0
        stored = None

        def create_if_absent(self, _key, stream, *, length, sha256, timeout_seconds, cancel_check):
            assert 0 < timeout_seconds <= 1 and not cancel_check()
            self.calls += 1
            self.stored = b"".join(iter(lambda: stream.read(1024), b""))
            if failure == "deadline":
                clock[0] = 2.0
            else:
                cancelled[0] = True
            assert cancel_check()
            return "created"

        def observe(self, _key, **_kwargs):
            self.observations += 1
            return len(self.stored), hashlib.sha256(self.stored).hexdigest()

    provider = Provider()
    with pytest.raises(transfer.TransferError, match="publish_uncertain"):
        transfer._publish_file_exact(
            source, destination=provider, key="opaque", length=len(data),
            sha256=hashlib.sha256(data).hexdigest(), refresh=lambda: None,
            deadline=1, cancel_check=lambda: cancelled[0],
        )
    assert provider.calls == 1 and provider.observations == 0
    assert provider.stored == data and source.read_bytes() == data


@pytest.mark.parametrize("phase", ["copy", "publish"])
def test_local_late_cancel_removes_only_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    source, output = tmp_path / "volume.lssc", tmp_path / "published.lssc"
    data = b"encrypted fixture" * 100_000
    source.write_bytes(data)
    source.chmod(0o600)
    cancelled = [False]
    if phase == "copy":
        real_read = os.read
        identity = source.stat()

        def read(descriptor, count):
            result = real_read(descriptor, count)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
                cancelled[0] = True
            return result

        monkeypatch.setattr(transfer.os, "read", read)
    else:
        publish = transfer.ExclusiveOutput.publish

        def publish_then_cancel(self, name):
            result = publish(self, name)
            cancelled[0] = True
            return result

        monkeypatch.setattr(transfer.ExclusiveOutput, "publish", publish_then_cancel)
    with pytest.raises(transfer.TransferError, match="cancelled"):
        transfer._publish_file_exact(
            source, destination=output, key="opaque", length=len(data),
            sha256=hashlib.sha256(data).hexdigest(), refresh=lambda: None,
            cancel_check=lambda: cancelled[0],
        )
    assert not output.exists() and source.read_bytes() == data
    assert set(tmp_path.iterdir()) == {source}


def test_real_transfer_resumes_only_ciphertext_and_rechecks_authority(
    separate_machine_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = separate_machine_env
    content = b"private fixture bytes\x00" * 9000
    prepared = _prepare(env, tmp_path, content=content)
    origin = prepared.volumes[0]
    with LocalFileRangeSource(origin.path) as source:
        approved = approve_container_distribution(
            source, output_dir=origin.path.parent,
            policy=env.policies["controller"],
            trust_state_path=env.paths["controller"],
            gnupg_home=env.homes["controller"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
    local = env.snapshots["origin"]
    context = CatalogContext(
        catalog_id="f" * 32, trust_state_path=env.paths["origin"],
        trust_store_id=local.store_id, expected_scope=SCOPE, role="publisher",
        trust_revision=local.revision, governance_epoch=local.epoch,
        signer_fingerprint=env.publisher,
        recipient_fingerprints=(env.publisher,), gnupg_home=env.homes["origin"],
        passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
    )
    assert context.trust_store_id != env.snapshots["receiver"].store_id
    cache = tmp_path / "cache"
    bounds = transfer.TransferLimits(
        chunk_bytes=64 * 1024, max_cached_bytes=1024 * 1024, max_chunks=16,
    )
    selected_policy = env.policies["receiver"]
    common = dict(
        cache_root=cache, local_context=context, policy=selected_policy,
        trust_state_path=env.paths["receiver"], gnupg_home=env.homes["receiver"],
        expected_key=approved.key, passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver, transfer_limits=bounds,
    )
    with LocalFileRangeSource(approved.path) as source:
        header = read_metadata(source)

        class MetadataSpy:
            @property
            def size(self):
                return source.size

            def read_range(self, offset, length):
                assert offset + length <= header.messages_offset + len(b"messages:\n")
                return source.read_range(offset, length)

        observed = transfer.inspect_transfer_metadata(
            MetadataSpy(), policy=selected_policy,
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert observed.payload_verified is False
        assert not cache.exists()
        budgeted = transfer.inspect_transfer_metadata(
            source, policy=selected_policy,
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            cancel_check=lambda: False,
        )
        assert budgeted.logical_commitment_sha256 == observed.logical_commitment_sha256
        with pytest.raises(transfer.TransferError, match="cancelled"):
            transfer.resume_selected_ciphertext(source, 0, cancel_check=lambda: True, **common)
        assert not cache.exists()

        class OutageSource:
            payload_reads = 0

            @property
            def size(self):
                return source.size

            @property
            def generation(self):
                return source.generation

            def read_range(self, offset, length):
                if offset >= header.messages_offset + len(b"messages:\n"):
                    self.payload_reads += 1
                    if self.payload_reads == 2:
                        raise OSError("fixture transport outage")
                return source.read_range(offset, length)

        outage = OutageSource()
        with pytest.raises(OSError, match="fixture transport outage"):
            transfer.resume_selected_ciphertext(outage, 0, **common)
        assert outage.payload_reads == 2
        sealed = runtime._read(cache / "progress.json")
        assert sealed is not None and set(sealed) == {"sealed"}
        assert content not in (cache / "progress.json").read_bytes()

        class ChangingGeneration:
            changed = False

            @property
            def size(self):
                return source.size

            @property
            def generation(self):
                return "changed" if self.changed else "initial"

            def read_range(self, offset, length):
                data = source.read_range(offset, length)
                if offset >= header.messages_offset + len(b"messages:\n"):
                    self.changed = True
                return data

        changing_cache = tmp_path / "generation-cache"
        with pytest.raises(transfer.TransferError, match="source_changed"):
            transfer.resume_selected_ciphertext(
                ChangingGeneration(), 0, **dict(common, cache_root=changing_cache),
            )
        assert not list(changing_cache.glob("*.cipherpart"))
        assert not (changing_cache / "selected.cipher").exists()

        result = transfer.resume_selected_ciphertext(source, 0, **common)
        assert result.reused_chunks == 1
        assert result.ciphertext_path.is_file()
        assert result.ciphertext_path.stat().st_mode & 0o077 == 0
        assert content not in result.ciphertext_path.read_bytes()
        assert transfer.resume_selected_ciphertext(source, 0, **common).reused_chunks > 0
        assert result.cached_bytes == sum(path.stat().st_size for path in cache.iterdir())
        encoded_size = observed.volume_record["messages"][0]["descriptor"]["serialized_length"]
        limited = replace(bounds, max_cached_bytes=encoded_size + 32 * 1024)
        constrained = dict(common, transfer_limits=limited, cache_root=tmp_path / "limited-cache")
        with pytest.raises(transfer.TransferError, match="cache_limit"):
            transfer.resume_selected_ciphertext(source, 0, **constrained)
        assert not (constrained["cache_root"] / "selected.cipher").exists()
        assert sum(path.stat().st_size for path in constrained["cache_root"].iterdir()) <= limited.max_cached_bytes
        preserved = {path.name: path.read_bytes() for path in cache.iterdir()}
        with pytest.raises(transfer.TransferError, match="cache_limit"):
            transfer.resume_selected_ciphertext(source, 0, **dict(common, transfer_limits=limited))
        assert {path.name: path.read_bytes() for path in cache.iterdir()} == preserved

        publication_dir = tmp_path / "published"
        publication_dir.mkdir(mode=0o700)
        destination = publication_dir / "exact.lssc"
        publication_args = dict(
            destination=destination, local_context=context, policy=selected_policy,
            trust_state_path=env.paths["receiver"], gnupg_home=env.homes["receiver"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert transfer.publish_prepared_volume(approved, **publication_args).status == "created"
        assert transfer.publish_prepared_volume(approved, **publication_args).status == "identical"
        assert destination.read_bytes() == approved.path.read_bytes()
        assert content not in destination.read_bytes()
        destination.write_bytes(b"different ciphertext")
        with pytest.raises(transfer.TransferError, match="ciphertext_collision"):
            transfer.publish_prepared_volume(approved, **publication_args)

        class Provider:
            data = None
            calls = 0
            lose_ack = False

            def create_if_absent(self, key, stream, *, length, sha256, timeout_seconds, cancel_check):
                assert 0 < timeout_seconds <= 3600 and not cancel_check()
                self.calls += 1
                if self.data is not None:
                    return "exists"
                self.data = b"".join(iter(lambda: stream.read(64 * 1024), b""))
                assert len(self.data) == length
                if self.lose_ack:
                    raise OSError("fixture lost provider acknowledgement")
                return "created"

            def observe(self, key, *, timeout_seconds, cancel_check):
                assert 0 < timeout_seconds <= 3600 and not cancel_check()
                if self.data is None:
                    return None
                import hashlib
                return len(self.data), hashlib.sha256(self.data).hexdigest()

        provider = Provider()
        provider.lose_ack = True
        provider_args = dict(publication_args, destination=provider)
        assert transfer.publish_prepared_volume(approved, **provider_args).status == "observed_after_uncertain"
        assert provider.calls == 1
        provider.lose_ack = False
        assert transfer.publish_prepared_volume(approved, **provider_args).status == "identical"
        assert provider.calls == 2
        provider.data = b"different ciphertext"
        with pytest.raises(transfer.TransferError, match="ciphertext_collision"):
            transfer.publish_prepared_volume(approved, **provider_args)

        # Public publication carries the exact full framed volume, including
        # encrypted metadata; selected embedded ciphertext stays in the cache.
        whole_path = publication_dir / "whole.lssc"
        whole_args = dict(
            local_context=context, policy=selected_policy,
            trust_state_path=env.paths["receiver"], gnupg_home=env.homes["receiver"],
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert transfer.publish_prepared_volume(
            approved, destination=whole_path, **whole_args,
        ).status == "created"
        assert whole_path.read_bytes() == approved.path.read_bytes()
        assert transfer.publish_prepared_volume(
            approved, destination=whole_path, **whole_args,
        ).status == "identical"
        with LocalFileRangeSource(whole_path) as delivered:
            delivered_metadata = transfer.inspect_transfer_metadata(
                delivered, policy=selected_policy,
                trust_state_path=env.paths["receiver"],
                gnupg_home=env.homes["receiver"], expected_key=approved.key,
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            )
            assert delivered_metadata.logical_commitment_sha256 == result.logical_commitment_sha256
            receipt = accept_container_metadata(
                delivered, policy=selected_policy,
                trust_state_path=env.paths["receiver"],
                gnupg_home=env.homes["receiver"], expected_key=approved.key,
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            )
            assert receipt.status in {"accepted", "already_accepted"}
        provider.data = None
        assert transfer.publish_prepared_volume(
            approved, destination=provider, **whole_args,
        ).status == "created"
        provider_metadata = transfer.inspect_transfer_metadata(
            BytesRangeSource(provider.data), policy=selected_policy,
            trust_state_path=env.paths["receiver"],
            gnupg_home=env.homes["receiver"], expected_key=approved.key,
            passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
        )
        assert provider_metadata.logical_commitment_sha256 == result.logical_commitment_sha256
        plain = tmp_path / "plain.lssc"
        plain.write_bytes(content)
        forged = replace(
            approved, path=plain, serialized_length=len(content),
            transport_sha256=hashlib.sha256(content).hexdigest(),
        )
        with pytest.raises(ArtifactError):
            transfer.publish_prepared_volume(
                forged, destination=publication_dir / "rejected.lssc", **whole_args,
            )
        assert not (publication_dir / "rejected.lssc").exists()

        class DifferentGeneration:
            @property
            def size(self):
                return source.size

            @property
            def generation(self):
                return "changed-provider-generation"

            def read_range(self, offset, length):
                return source.read_range(offset, length)

        with pytest.raises(transfer.TransferError, match="progress_mismatch"):
            transfer.resume_selected_ciphertext(DifferentGeneration(), 0, **common)

        progress = transfer._open_progress(runtime._read(cache / "progress.json"), context)
        part = transfer._chunk_path(cache, progress["attempt"], 0)
        original_part = part.read_bytes()
        part.write_bytes(b"X" + original_part[1:])
        with pytest.raises(transfer.TransferError, match="cache_invalid"):
            transfer.resume_selected_ciphertext(source, 0, **common)
        part.write_bytes(original_part)

        original_output = result.ciphertext_path.read_bytes()
        result.ciphertext_path.write_bytes(b"different ciphertext")
        with pytest.raises(transfer.TransferError, match="ciphertext_collision"):
            transfer.resume_selected_ciphertext(source, 0, **common)
        result.ciphertext_path.write_bytes(original_output)

        tampered = runtime._read(cache / "progress.json")
        assert tampered is not None
        tampered["sealed"] = tampered["sealed"][:-2] + "AA"
        runtime._write(cache / "progress.json", tampered)
        with pytest.raises(transfer.TransferError, match="progress_invalid"):
            transfer.resume_selected_ciphertext(source, 0, **common)
        runtime._write(cache / "progress.json", sealed)

        # A lost return after the create-only link is reconciled by exact digest.
        other = dict(common, cache_root=tmp_path / "lost-ack-cache")
        publish = transfer.ExclusiveOutput.publish

        def lost_ack(self, name):
            value = publish(self, name)
            if self.path.name == "selected.cipher":
                raise OSError("fixture lost acknowledgement")
            return value

        with monkeypatch.context() as patch:
            patch.setattr(transfer.ExclusiveOutput, "publish", lost_ack)
            observed = transfer.resume_selected_ciphertext(source, 0, **other)
        assert observed.ciphertext_path.is_file()
        assert observed.ciphertext_sha256 == result.ciphertext_sha256

        # A currently changed local authority cannot reopen or resume progress.
        with sqlite3.connect(env.paths["origin"]) as db:
            db.execute("UPDATE state SET revision=revision+1 WHERE singleton=1")
        with pytest.raises(transfer.TransferError, match="local_authority_changed"):
            transfer.resume_selected_ciphertext(source, 0, **common)
