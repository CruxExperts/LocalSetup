"""Complete-file release, rather than merely verified individual segments."""

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from ls.core.openpgp import container_reconstruction as reconstruction
from ls.core.openpgp import artifacts, trust_state
from ls.core.openpgp.container_packing import PackingLimits
from ls.core.openpgp.container_preparation import ContainerInput, ContainerNamespace, ContainerPreparationLimits
from ls.core.openpgp.container_sources import LocalFileRangeSource
from ls.tests.test_openpgp_artifacts import SECRET_REFERENCE, _prepare, separate_machine_env


def test_reconstruction_denies_before_reading_sources(tmp_path):
    with pytest.raises(artifacts.ArtifactError) as denied:
        reconstruction.extract_container_file({}, "b" * 32, "output", authorized=False,
                                              policy=None, trust_state_path=tmp_path,
                                              gnupg_home=tmp_path)
    assert denied.value.code == artifacts.ArtifactErrorCode.AUTHORIZATION_DENIED
    assert not list(tmp_path.iterdir())


def test_independently_valid_containers_cannot_be_mixed(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (other / "origin").mkdir(mode=0o700)
    prepared = (_prepare(env, tmp_path, content=b"first file"),
                _prepare(env, other, content=b"different file"))
    with ExitStack() as stack:
        streams = {}
        for value in prepared:
            volume = value.volumes[0]
            with LocalFileRangeSource(volume.path) as source:
                approved = artifacts.approve_container_distribution(
                    source, output_dir=volume.path.parent, policy=env.policies["controller"],
                    trust_state_path=env.paths["controller"], gnupg_home=env.homes["controller"],
                    passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
                )
            streams[approved.key] = stack.enter_context(LocalFileRangeSource(approved.path))
        monkeypatch.setattr(artifacts, "_copy_selected_file", lambda *_a, **_kw: pytest.fail("mixed payload read"))
        with pytest.raises(reconstruction.ReconstructionError, match="conflicting_volumes"):
            reconstruction.extract_container_file(
                streams, "b" * 32, "mixed.bin", authorized=True, policy=env.policies["receiver"],
                trust_state_path=env.paths["receiver"], gnupg_home=env.homes["receiver"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            )
    boundary = Path(env.policies["receiver"].boundary)
    assert not (boundary / "mixed.bin").exists() and not list(boundary.glob(".lssc-file-*"))


def test_real_multivolume_reconstruction_and_release_guards(tmp_path, separate_machine_env, monkeypatch):
    env = separate_machine_env
    payload = bytes(range(256)) * 120
    source = tmp_path / "input.bin"
    source.write_bytes(payload)
    output = tmp_path / "volumes"
    output.mkdir(mode=0o700)
    prepared = artifacts.prepare_container(
        (ContainerInput(source, "b" * 32),), output_dir=output,
        namespace=ContainerNamespace("tenant", "project", "a" * 32),
        policy=env.policies["origin"], trust_state_path=env.paths["origin"],
        gnupg_home=env.homes["origin"], passphrase_reference=SECRET_REFERENCE,
        secret_resolver=env.resolver,
        limits=ContainerPreparationLimits(packing_limits=PackingLimits(
            target_bytes=25_000, tolerance=.1, max_split_iterations=12, max_measure_calls=200)),
    )
    assert len(prepared.volumes) > 1
    approved = []
    for volume in prepared.volumes:
        with LocalFileRangeSource(volume.path) as stream:
            approved.append(artifacts.approve_container_distribution(
                stream, output_dir=output, policy=env.policies["controller"],
                trust_state_path=env.paths["controller"], gnupg_home=env.homes["controller"],
                passphrase_reference=SECRET_REFERENCE, secret_resolver=env.resolver,
            ))
    policy = env.policies["receiver"]
    boundary = Path(policy.boundary)
    common = dict(authorized=True, policy=policy, trust_state_path=env.paths["receiver"],
                  gnupg_home=env.homes["receiver"], passphrase_reference=SECRET_REFERENCE,
                  secret_resolver=env.resolver)
    with ExitStack() as stack:
        streams = {volume.key: stack.enter_context(LocalFileRangeSource(volume.path))
                   for volume in reversed(approved)}
        with monkeypatch.context() as patch:
            patch.setattr(artifacts, "_copy_selected_file", lambda *_a, **_kw: pytest.fail("incomplete payload read"))
            with pytest.raises(reconstruction.ReconstructionError, match="incomplete"):
                reconstruction.extract_container_file(dict(list(streams.items())[:-1]), "b" * 32,
                                                      "missing.bin", **common)
        assert not (boundary / "missing.bin").exists()
        receipt = reconstruction.extract_container_file(streams, "b" * 32, "complete.bin", **common)
        assert receipt.segment_count >= len(approved)
        assert receipt.plaintext_length == len(payload)
        assert receipt.plaintext_sha256 == hashlib.sha256(payload).hexdigest()
        assert (boundary / "complete.bin").read_bytes() == payload
        assert (boundary / "complete.bin").stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(env.paths["receiver"]) as db:
            assert db.execute("SELECT count(*) FROM consumed WHERE kind='container-file-v1'").fetchone()[0] == 1

        record = trust_state._record_accepted_container
        for failure in ("digest", "receipt", "authority", "cancel", "expiry", "late_cancel"):
            cancelled = [False]

            def intercept(path, **kwargs):
                if kwargs.get("required_selected_receipts"):
                    if failure == "receipt":
                        kwargs["required_selected_receipts"] = ("f" * 64,)
                    elif failure == "authority":
                        kwargs["expected_revision"] += 1
                    elif failure == "digest":
                        # Alter the private assembly after opening, before the
                        # final writer-lock check; no wrong bytes may escape.
                        assembly = next(boundary.glob(".lssc-file-*/assembled.plain"))
                        with assembly.open("r+b") as file:
                            file.write(b"changed")
                            file.flush()
                            os.fsync(file.fileno())
                    elif failure == "cancel":
                        cancelled[0] = True
                return record(path, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(trust_state, "_record_accepted_container", intercept)
                if failure == "expiry":
                    original_hash = reconstruction.hash_file

                    def expire_after_final_hash(*args, **kwargs):
                        result = original_hash(*args, **kwargs)
                        patch.setattr(artifacts, "_clock", lambda: policy.expires_at + 1)
                        return result

                    patch.setattr(reconstruction, "hash_file", expire_after_final_hash)
                if failure == "late_cancel":
                    original_copy = artifacts._copy_verified_payload

                    def cancel_after_publication(*args, **kwargs):
                        original_copy(*args, **kwargs)
                        cancelled[0] = True

                    patch.setattr(artifacts, "_copy_verified_payload", cancel_after_publication)
                with pytest.raises((reconstruction.ReconstructionError, artifacts.ArtifactError)):
                    reconstruction.extract_container_file(
                        streams, "b" * 32, failure + ".bin", **common,
                        limits=replace(ContainerPreparationLimits(), cancel_check=lambda: cancelled[0]),
                    )
            assert not (boundary / (failure + ".bin")).exists()
            assert not list(boundary.glob(".lssc-file-*"))
        assert (boundary / "complete.bin").read_bytes() == payload
        with pytest.raises(reconstruction.ReconstructionError):
            reconstruction.extract_container_file(
                streams, "b" * 32, "over-budget.bin", **common,
                limits=replace(ContainerPreparationLimits(), max_temporary_bytes=2 * len(payload) - 1),
            )
        assert not (boundary / "over-budget.bin").exists()
        assert not list(boundary.glob(".lssc-file-*"))
