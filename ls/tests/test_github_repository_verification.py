from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

import ls.core.github_repo.verification as verification


_PRIMARY = "A" * 40
_OTHER = "B" * 40
_PUBLIC_KEY = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n-----END PGP PUBLIC KEY BLOCK-----"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _repository(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Verification Fixture")
    _git(root, "config", "user.email", "verification@example.invalid")
    # The verifier must supply its own fixed OpenPGP program for verify commands.
    _git(root, "config", "gpg.program", "/fixture/forbidden-gpg")
    (root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def _validsig(primary: str = _PRIMARY, signing: str | None = None) -> bytes:
    signer = signing or primary
    suffix = f" {primary}" if signer != primary else ""
    return f"[GNUPG:] VALIDSIG {signer} 2026-09-26 1727350000 0 4 0 22 8 00{suffix}\n".encode("ascii")


def _stub_gpg_and_signature_checks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit_status: bytes | None = None,
    commit_code: int = 0,
    tag_status: bytes | None = None,
    tag_code: int = 0,
    secret_keyring: bool = False,
) -> list[tuple[list[str], dict[str, str]]]:
    real_discover = verification._discover_executable
    real_run = verification._run_bounded
    invocations: list[tuple[list[str], dict[str, str]]] = []

    def fake_discover(name: str, checkout_root: Path) -> str | None:
        if name == "gpg":
            return "/fixture/gpg"
        return real_discover(name, checkout_root)

    def fake_run(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin: Any = subprocess.DEVNULL,
    ) -> tuple[bytes, bytes, int]:
        invocations.append((list(args), dict(env or {})))
        if args[0] == "/fixture/gpg":
            if "--import" in args:
                if env is not None:
                    invocations[-1][1]["GPG_CONF"] = (Path(env["GNUPGHOME"]) / "gpg.conf").read_text(encoding="ascii")
                return b"", b"", 0
            if "--list-keys" in args:
                return f"pub:::::::::\nfpr:::::::::{_PRIMARY}\n".encode("ascii"), b"", 0
            if "--list-secret-keys" in args:
                secret_records = b"sec:::::::::\n" if secret_keyring else b""
                return secret_records, b"", 0
            raise AssertionError(f"unexpected GPG operation: {args}")
        if "verify-commit" in args:
            return b"", commit_status if commit_status is not None else _validsig(), commit_code
        if "verify-tag" in args:
            return b"", tag_status if tag_status is not None else _validsig(), tag_code
        return real_run(args, cwd=cwd, env=env, stdin=stdin)

    monkeypatch.setattr(verification, "_discover_executable", fake_discover)
    monkeypatch.setattr(verification, "_run_bounded", fake_run)
    return invocations


def test_verifies_exact_commit_and_annotated_tag_in_isolated_trust_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "signed-evidence-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    calls = _stub_gpg_and_signature_checks(monkeypatch)

    result = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result == {
        "status": "verified",
        "reason": None,
        "evidence_scope": "local_git_openpgp_signatures",
        "commit_object_id": commit_oid,
        "tag_name": "v1.0.0",
        "tag_object_id": _git(root, "rev-parse", "refs/tags/v1.0.0^{tag}"),
        "commit_primary_fingerprint": _PRIMARY,
        "tag_primary_fingerprint": _PRIMARY,
    }
    verify_calls = [(args, env) for args, env in calls if "verify-commit" in args or "verify-tag" in args]
    assert len(verify_calls) == 2
    for args, env in verify_calls:
        assert f"gpg.program=/fixture/gpg" in args
        assert env["GNUPGHOME"] == env["HOME"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_NO_LAZY_FETCH"] == "1"
        assert "--no-replace-objects" in args
        assert "GIT_CONFIG_COUNT" not in env
        assert "/fixture/forbidden-gpg" not in " ".join(args)
    import_env = next(env for args, env in calls if args[0] == "/fixture/gpg" and "--import" in args)
    assert "no-auto-key-retrieve" in import_env["GPG_CONF"]
    assert str(root) not in json.dumps(result)


def test_replacement_refs_cannot_change_the_selected_commit_or_tag_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "replacement-ref-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    (root / "tracked.txt").write_text("replacement commit\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "replacement")
    replacement_oid = _git(root, "rev-parse", "HEAD")
    _git(root, "replace", commit_oid, replacement_oid)
    _stub_gpg_and_signature_checks(monkeypatch)

    result = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result["status"] == "verified"
    assert result["commit_object_id"] == commit_oid
    assert result["tag_name"] == "v1.0.0"


@pytest.mark.parametrize("tool_name", ["git", "gpg"])
def test_discovery_skips_relative_and_checkout_local_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    root = tmp_path / "repository"
    checkout_bin = root / "bin"
    relative_bin = tmp_path / "relative-bin"
    safe_bin = tmp_path / "safe-bin"
    checkout_bin.mkdir(parents=True)
    relative_bin.mkdir()
    safe_bin.mkdir()
    marker = tmp_path / "shim-ran"
    shim_text = f"#!/bin/sh\ntouch {marker}\n"
    for directory in (checkout_bin, relative_bin, safe_bin):
        executable = directory / tool_name
        executable.write_text(shim_text, encoding="utf-8")
        executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(("relative-bin", os.fspath(checkout_bin), os.fspath(safe_bin))))

    selected = verification._discover_executable(tool_name, root.resolve())

    assert selected == str((safe_bin / tool_name).resolve())
    assert not marker.exists()


def test_discovery_ignores_relative_path_when_no_absolute_entry_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repository"
    relative_bin = tmp_path / "relative-bin"
    relative_bin.mkdir(parents=True)
    executable = relative_bin / "git"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "relative-bin")

    assert verification._discover_executable("git", root) is None


def test_rejects_invalid_object_id_before_running_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_gpg_and_signature_checks(monkeypatch)

    result = verification.verify_release_signatures(tmp_path, "HEAD", "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result["status"] == "invalid"
    assert result["reason"] == "commit_object_id_invalid"
    assert not calls


@pytest.mark.parametrize(
    ("commit_status", "commit_code", "expected_reason"),
    [
        (b"[GNUPG:] NO_PUBKEY missing\n", 1, "commit_signature_invalid"),
        (_validsig(primary=_OTHER), 0, "signature_signer_not_trusted"),
        (b"[GNUPG:] EXPKEYSIG " + _OTHER.encode("ascii") + b" expired\n" + _validsig(), 0, "signature_invalid"),
    ],
)
def test_rejects_unsigned_or_untrusted_commit_signatures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_status: bytes,
    commit_code: int,
    expected_reason: str,
) -> None:
    root = tmp_path / "signature-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    _stub_gpg_and_signature_checks(monkeypatch, commit_status=commit_status, commit_code=commit_code)

    result = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result["status"] == "invalid"
    assert result["reason"] == expected_reason


def test_missing_and_malformed_trust_inputs_are_explicit(tmp_path: Path) -> None:
    missing = verification.verify_release_signatures(tmp_path, "a" * 40, "v1.0.0", None, None)
    malformed = verification.verify_release_signatures(tmp_path, "a" * 40, "v1.0.0", [b"not an armored key"], [_PRIMARY])

    assert missing["status"] == "incomplete"
    assert missing["reason"] == "trusted_key_input_missing"
    assert malformed["status"] == "invalid"
    assert malformed["reason"] == "trusted_key_input_invalid"


def test_trusted_public_key_files_are_read_separately_and_paths_are_redacted(tmp_path: Path) -> None:
    key_path = tmp_path / "trusted-public-key.asc"
    key_path.write_bytes(_PUBLIC_KEY)
    assert verification.load_trusted_public_keys([key_path]) == [_PUBLIC_KEY]

    link_path = tmp_path / "key-link.asc"
    link_path.symlink_to(key_path)
    with pytest.raises(ValueError) as caught:
        verification.load_trusted_public_keys([link_path])
    assert str(link_path) not in str(caught.value)


def test_rejects_imported_secret_key_material_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "secret-key-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    _stub_gpg_and_signature_checks(monkeypatch, secret_keyring=True)

    result = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result["status"] == "invalid"
    assert result["reason"] == "trusted_secret_key_material_invalid"


def test_requires_annotated_tag_and_exact_selected_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tag-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "v1.0.0")
    _stub_gpg_and_signature_checks(monkeypatch)

    lightweight = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert lightweight["status"] == "invalid"

    _git(root, "tag", "-d", "v1.0.0")
    (root / "tracked.txt").write_text("other commit\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "other")
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    wrong_target = verification.verify_release_signatures(root, commit_oid, "v1.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert wrong_target["status"] == "invalid"
    assert wrong_target["reason"] == "annotated_tag_target_mismatch"


def test_annotated_tag_header_must_match_selected_ref_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "renamed-tag-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "-a", "v1.0.0", "-m", "release")
    tag_oid = _git(root, "rev-parse", "refs/tags/v1.0.0^{tag}")
    _git(root, "update-ref", "refs/tags/v2.0.0", tag_oid)
    _stub_gpg_and_signature_checks(monkeypatch)

    result = verification.verify_release_signatures(root, commit_oid, "v2.0.0", [_PUBLIC_KEY], [_PRIMARY])

    assert result["status"] == "invalid"
    assert result["reason"] == "annotated_tag_name_mismatch"


def test_verifies_artifact_digest_bound_to_caller_release_identity(tmp_path: Path) -> None:
    root = tmp_path / "artifact-fixture"
    commit_oid = _repository(root)
    _git(root, "tag", "v1.0.0")
    artifact = b"release artifact bytes"
    (root / "dist").mkdir()
    (root / "dist" / "package.tar.gz").write_bytes(artifact)

    result = verification.verify_release_artifacts(
        root,
        {
            "repository_id": 42, "repository_name": "Owner/Repo", "release_id": 73,
            "tag_name": "v1.0.0", "source_ref": "refs/tags/v1.0.0", "source_commit": commit_oid,
        },
        [{"asset_id": 12, "name": "package.tar.gz", "relative_path": "dist/package.tar.gz", "expected_sha256": hashlib.sha256(artifact).hexdigest()}],
    )

    assert result == {
        "status": "incomplete",
        "reason": "independent_release_proof_missing",
        "evidence_scope": "local_file_digest_match_only",
        "release_identity": {
            "repository_id": 42, "repository_name": "Owner/Repo", "release_id": 73,
            "tag_name": "v1.0.0", "source_ref": "refs/tags/v1.0.0", "source_commit": commit_oid,
        },
        "artifacts": [{
            "status": "matched",
            "reason": None,
            "asset_id": 12,
            "name": "package.tar.gz",
            "relative_path": "dist/package.tar.gz",
            "sha256": hashlib.sha256(artifact).hexdigest(),
            "size_bytes": len(artifact),
        }],
    }


def test_rejects_artifact_digest_mismatch_and_escape_or_symlink(tmp_path: Path) -> None:
    root = tmp_path / "artifact-rejections"
    commit_oid = _repository(root)
    _git(root, "tag", "v2.0.0")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "linked.bin").symlink_to(outside)
    (root / "present.bin").write_bytes(b"inside")
    identity = {
        "release_id": 91, "tag_name": "v2.0.0", "source_ref": "refs/tags/v2.0.0",
        "source_commit": commit_oid,
    }

    mismatch = verification.verify_release_artifacts(
        root,
        {"repository_id": 47, "repository_name": "Owner/Repo", **identity},
        [{"asset_id": 19, "name": "present.bin", "relative_path": "present.bin", "expected_sha256": "0" * 64}],
    )
    unsafe = verification.verify_release_artifacts(
        root,
        {"repository_id": 47, "repository_name": "Owner/Repo", **identity},
        [
            {"asset_id": 20, "name": "outside.bin", "relative_path": "../outside.bin", "expected_sha256": hashlib.sha256(b"outside").hexdigest()},
            {"asset_id": 21, "name": "linked.bin", "relative_path": "linked.bin", "expected_sha256": hashlib.sha256(b"outside").hexdigest()},
        ],
    )

    assert mismatch["status"] == "invalid"
    assert mismatch["artifacts"][0]["reason"] == "artifact_digest_mismatch"
    assert unsafe["status"] == "invalid"
    assert unsafe["artifacts"][0]["reason"] == "asset_path_invalid"
    assert unsafe["artifacts"][1]["reason"] == "symlink_not_allowed"
    assert str(tmp_path) not in json.dumps(unsafe)


def test_rejects_oversized_artifacts_and_missing_expectations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "bounded-artifacts"
    commit_oid = _repository(root)
    _git(root, "tag", "v3.0.0")
    (root / "large.bin").write_bytes(b"too large")
    monkeypatch.setattr(verification, "_MAX_ARTIFACT_BYTES", 4)

    oversized = verification.verify_release_artifacts(
        root,
        {
            "repository_id": 14, "repository_name": "Owner/Repo", "release_id": 14,
            "tag_name": "v3.0.0", "source_ref": "refs/tags/v3.0.0", "source_commit": commit_oid,
        },
        [{"asset_id": 14, "name": "large.bin", "relative_path": "large.bin", "expected_sha256": hashlib.sha256(b"too large").hexdigest()}],
    )
    absent = verification.verify_release_artifacts(
        root, {
            "repository_id": 14, "repository_name": "Owner/Repo", "release_id": 14,
            "tag_name": "v3.0.0", "source_ref": "refs/tags/v3.0.0", "source_commit": commit_oid,
        }, [],
    )

    assert oversized["status"] == "invalid"
    assert oversized["artifacts"][0]["reason"] == "artifact_size_limit_exceeded"
    assert absent["status"] == "incomplete"
    assert absent["reason"] == "artifact_expectations_missing"
