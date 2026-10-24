from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from datetime import date, datetime, time as daytime, timezone

import pytest

from ls.core.openpgp import (
    EnvelopePolicy,
    KeyCapability,
    KeyProfile,
    LocalTrust,
    NativeInputContext,
    NativeInputError,
    NativeInputErrorCode,
    SecretReference,
    SecretResolutionError,
    SecretResolutionErrorCode,
    SecretResolver,
    describe_open_operation,
    describe_enrollment_operation,
    describe_seal_operation,
    expected_expiry_date,
)
from ls.core.openpgp.native_input import _valid_passphrase
from ls.core.openpgp.key_records import KeyInspection, KeyRecord, _INSPECTION_SEAL
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.dialog_model import Response


SIGNER = "A" * 40
RECIPIENT = "B" * 40


class _DialogService(ShellCommandService):
    def __init__(self, *, outcome: str = "delivered", input_value: bytes = b"test-passphrase") -> None:
        self.outcome = outcome
        self.input_value = input_value
        self.current = True
        self.responses: list[Response] = []
        self.forms = []
        self.after_delivery = None

    def _load(self, handle):
        return Path("/tmp"), {
            "handle": handle,
            "endpoint": "endpoint-test",
            "machine_generation": "generation-test",
            "owner_fingerprint": "owner-test",
            "native_session_id": "session-test",
            "layout_mode": "canvas-v3",
            "state": "ready" if self.current else "exited",
        }, None

    def _authorize(self, _session, _owner):
        return None

    def _family_caller(self, _session, owner, *, require_root_live):
        assert require_root_live is True
        return owner, "root", "root", "owner-test"

    def _verify(self, _session):
        return {"state": "ready" if self.current else "unavailable"}

    def dialog(self, handle, *, operation_id, form, deadline, validate_current, deliver, owner):
        self.forms.append(form)
        if self.outcome != "delivered":
            return {
                "state": self.outcome,
                "effect_certainty": "none" if self.outcome != "delivery_unknown" else "unknown",
                "ok": self.outcome in {"cancelled", "expired"},
                "cleanup_pending": False,
                "handle": handle,
                "operation_id": operation_id,
            }
        assert validate_current() is True
        field = form.fields[0]
        payload = self.input_value if field.kind == "password" else field.confirmation.encode("ascii")
        response = Response({field.name: bytearray(payload)})
        self.responses.append(response)
        accepted = deliver(response)
        if self.after_delivery is not None:
            self.after_delivery()
        return {
            "state": "delivered" if accepted else "delivery_unknown",
            "effect_certainty": "known" if accepted else "unknown",
            "ok": accepted,
            "cleanup_pending": False,
            "handle": handle,
            "operation_id": operation_id,
        }


def _operation(home: Path, *, payload: bytes = b"fixture payload", disable_compression: bool = False):
    return describe_seal_operation(
        payload=payload,
        signer_fingerprint=SIGNER,
        recipient_fingerprints=(RECIPIENT,),
        local_trust=LocalTrust({SIGNER}),
        gnupg_home=home,
        disable_compression=disable_compression,
    )


def _context(
    expected,
    *,
    service: _DialogService | None = None,
    deadline: float | None = None,
    validate_current=None,
):
    service = service or _DialogService()
    context = NativeInputContext(
        service=service,
        handle="native-test-handle",
        owner={"pid": os.getpid(), "uid": os.getuid()},
        operation_id="openpgp-native-test",
        expected=expected,
        deadline=time.monotonic() + 30 if deadline is None else deadline,
        validate_current=(lambda: True) if validate_current is None else validate_current,
    )
    return context, service


def test_operation_descriptors_bind_exact_bytes_and_normalized_inputs(tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)

    first = _operation(home, payload=b"first")
    same_set_order = describe_seal_operation(
        payload=b"first",
        signer_fingerprint=f"{SIGNER[:10]} {SIGNER[10:]}",
        recipient_fingerprints=(f"{RECIPIENT[:20]} {RECIPIENT[20:]}",),
        local_trust=LocalTrust({SIGNER.lower()}),
        gnupg_home=home,
    )
    different_payload = _operation(home, payload=b"second")
    no_compression = _operation(home, payload=b"first", disable_compression=True)

    assert first == same_set_order
    assert first.digest != different_payload.digest
    assert first.digest != no_compression.digest
    assert "first" not in first.summary
    assert first.summary.endswith("compression enabled.")

    policy = EnvelopePolicy({SIGNER}, {RECIPIENT}, publisher_fingerprint=SIGNER)
    opened = describe_open_operation(
        serialized=b"opaque serialized envelope",
        policy=policy,
        local_trust=LocalTrust({SIGNER}),
        gnupg_home=home,
    )
    changed_policy = describe_open_operation(
        serialized=b"opaque serialized envelope",
        policy=EnvelopePolicy({RECIPIENT}, {RECIPIENT}, publisher_fingerprint=SIGNER),
        local_trust=LocalTrust({SIGNER}),
        gnupg_home=home,
    )
    assert opened.purpose == "open-envelope"
    assert opened.digest != changed_policy.digest
    assert "allowed signers" in opened.summary


def test_passphrase_mismatch_is_rejected_before_dialog_and_context_is_one_use(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    expected = _operation(home)
    wrong = _operation(home, payload=b"another payload")
    context, service = _context(expected)

    with pytest.raises(NativeInputError) as mismatch:
        context.request_passphrase(wrong)
    assert mismatch.value.code == NativeInputErrorCode.OPERATION_MISMATCH
    assert service.forms == []
    context.checkpoint(expected)

    buffer = context.request_passphrase(expected)

    assert buffer == b"test-passphrase"
    assert isinstance(buffer, bytearray)
    assert len(service.forms) == 1
    assert service.forms[0].title == "Unlock signing key"
    assert service.responses[0].values == {}
    context.checkpoint(expected)
    with pytest.raises(NativeInputError) as replay:
        context.request_passphrase(expected)
    assert replay.value.code == NativeInputErrorCode.INPUT_ALREADY_USED
    context.finish()
    buffer[:] = b"\x00" * len(buffer)
    buffer.clear()


def test_concurrent_direct_request_cannot_close_the_waiting_winner(tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    operation = _operation(home)
    entered = threading.Event()
    proceed = threading.Event()
    block = [False]

    def validate_current() -> bool:
        if block[0]:
            entered.set()
            assert proceed.wait(5)
        return True

    context, service = _context(operation, validate_current=validate_current)
    block[0] = True
    result: list[bytearray | Exception] = []

    def winner() -> None:
        try:
            result.append(context.request_passphrase(operation))
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=winner)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(NativeInputError) as loser:
            context.request_passphrase(operation)
        assert loser.value.code is NativeInputErrorCode.INPUT_ALREADY_USED
        assert context._status == "requesting"
        assert service.forms == []
    finally:
        proceed.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(result) == 1 and isinstance(result[0], bytearray)
    assert result[0] == b"test-passphrase"
    assert len(service.forms) == 1
    with pytest.raises(NativeInputError) as reused:
        context.request_passphrase(operation)
    assert reused.value.code is NativeInputErrorCode.INPUT_ALREADY_USED
    context.finish()
    result[0][:] = b"\x00" * len(result[0])
    result[0].clear()


def test_consumer_lease_prevents_competing_finish_and_sequential_reuse(tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    operation = _operation(home)
    context, service = _context(operation)
    lease = context.claim(operation)
    rejected: list[NativeInputErrorCode] = []

    def competitor() -> None:
        try:
            context.claim(operation)
        except NativeInputError as exc:
            rejected.append(exc.code)
        try:
            context.request_passphrase(operation)
        except NativeInputError as exc:
            rejected.append(exc.code)
        context.finish()
        context.finish(object())

    thread = threading.Thread(target=competitor)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert rejected == [NativeInputErrorCode.INPUT_ALREADY_USED] * 2
    assert context._status == "fresh"
    secret = context.request_passphrase(operation)
    assert secret == b"test-passphrase"
    assert len(service.forms) == 1
    context.finish(lease)
    assert context._status == "closed"
    with pytest.raises(NativeInputError) as reused:
        context.claim(operation)
    assert reused.value.code is NativeInputErrorCode.INPUT_ALREADY_USED
    secret[:] = b"\x00" * len(secret)
    secret.clear()


@pytest.mark.parametrize("outcome", ["cancelled", "expired", "delivery_unknown"])
def test_cancel_expiry_and_unknown_delivery_clear_the_owned_handoff(
    tmp_path: Path, outcome: str
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    context, service = _context(_operation(home), service=_DialogService(outcome=outcome))

    with pytest.raises(NativeInputError):
        context.request_passphrase(context.expected)

    assert context._status == "closed"
    assert context._handoff is None
    assert len(service.forms) == 1


def test_owner_loss_after_delivery_discards_copy_and_original_response(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    service = _DialogService()
    service.after_delivery = lambda: setattr(service, "current", False)
    current = [True]
    context, _service = _context(
        _operation(home), service=service, validate_current=lambda: current[0]
    )
    service.after_delivery = lambda: current.__setitem__(0, False)

    with pytest.raises(NativeInputError) as owner_lost:
        context.request_passphrase(context.expected)

    assert owner_lost.value.code == NativeInputErrorCode.OWNER_UNAVAILABLE
    assert context._handoff is None
    assert service.responses[0].values == {}


def test_revocation_during_delivery_wipes_the_untransferred_buffer(tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    service = _DialogService()
    context, _service = _context(_operation(home), service=service)
    service.after_delivery = context.finish

    with pytest.raises(NativeInputError) as revoked:
        context.request_passphrase(context.expected)

    assert revoked.value.code is NativeInputErrorCode.INPUT_UNAVAILABLE
    assert context._status == "closed"
    assert context._handoff is None
    assert service.responses[0].values == {}


def test_input_deadline_expiring_after_delivery_discards_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ls.core.openpgp.native_input as native_input_module

    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    now = [1.0]
    monkeypatch.setattr(native_input_module.time, "monotonic", lambda: now[0])
    service = _DialogService()
    service.after_delivery = lambda: now.__setitem__(0, 11.0)
    context, _service = _context(_operation(home), service=service, deadline=10.0)

    with pytest.raises(NativeInputError) as expired:
        context.request_passphrase(context.expected)

    assert expired.value.code == NativeInputErrorCode.DEADLINE_EXPIRED
    assert context._handoff is None
    assert service.responses[0].values == {}


def test_resolver_native_source_never_falls_back_or_accepts_a_second_source(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    operation = _operation(home)
    context, service = _context(operation)
    reference = SecretReference("env", "OPENPGP_PASSPHRASE")
    resolver = SecretResolver(env_values={reference.name: "fallback-must-not-be-used"})

    with pytest.raises(SecretResolutionError) as no_source:
        resolver.resolve_buffer(None, operation=operation)
    assert no_source.value.code is SecretResolutionErrorCode.INVALID_REFERENCE
    with pytest.raises(SecretResolutionError) as two_sources:
        resolver.resolve_buffer(reference, operation=operation, native_input=context)
    assert two_sources.value.code is SecretResolutionErrorCode.INVALID_REFERENCE
    assert service.forms == []

    secret = resolver.resolve_buffer(None, operation=operation, native_input=context)
    assert secret == b"test-passphrase"
    assert secret != b"fallback-must-not-be-used"
    assert service.forms[0].title == "Unlock signing key"
    context.finish()
    secret[:] = b"\x00" * len(secret)
    secret.clear()


def test_enrollment_dialog_requires_the_inspected_full_fingerprint(tmp_path: Path) -> None:
    home = tmp_path / "gpg-home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    today = date.today()
    expires = expected_expiry_date(today)
    expires_epoch = int(
        datetime.combine(expires, daytime.min, tzinfo=timezone.utc).timestamp()
    )
    record = KeyRecord(
        fingerprint=SIGNER,
        algorithm="RSA",
        bits=4096,
        capabilities=frozenset({KeyCapability.SIGN}),
        created_on=today,
        expires_on=expires,
        is_primary=True,
        expires_epoch=expires_epoch,
        direct_capabilities=frozenset({KeyCapability.SIGN}),
    )
    inspection = KeyInspection(record, (), _INSPECTION_SEAL)
    operation = describe_enrollment_operation(
        inspection=inspection,
        expected_fingerprint=SIGNER,
        required_capabilities={KeyCapability.SIGN},
        profile=KeyProfile(),
        local_trust=LocalTrust(),
    )
    context, service = _context(operation)

    context.confirm_enrollment(
        operation,
        fingerprint=SIGNER,
        description=operation.summary,
    )

    form = service.forms[0]
    assert form.title == "Enroll certificate"
    assert form.fields[0].kind == "confirm"
    assert form.fields[0].confirmation == SIGNER
    assert service.responses[0].values == {}
    assert "as signer" in form.description
    context.checkpoint(operation)
    context.finish()


@pytest.mark.parametrize(
    "value",
    [bytearray(b""), bytearray(b"bad\x00secret"), bytearray(b"bad\nsecret"), bytearray(b"\xff")],
)
def test_passphrase_handoff_validates_bounded_utf8_without_string_conversion(value: bytearray) -> None:
    assert _valid_passphrase(value) is False
