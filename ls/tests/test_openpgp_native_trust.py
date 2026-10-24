"""Exact, one-use persistent authority confirmation and publication failures."""

from datetime import date, datetime, timezone
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time
from threading import Event

import pytest

from ls.core.openpgp import (
    KeyCapability, NativeInputError, NativeInputErrorCode,
    describe_trust_initialization_operation, initialize_trust_state, load_trust_state,
)
from ls.core.openpgp import envelope_io, trust_initialization as initialization, trust_state
from ls.core.openpgp.key_records import KeyInspection, KeyRecord, _INSPECTION_SEAL
from ls.core.openpgp.transition_contracts import TransitionError
from test_openpgp_native_input import _DialogService, _context


class TrustDialog(_DialogService):
    def __init__(self, path, *, corrupt=None, outcome="delivered"):
        super().__init__(outcome=outcome)
        self.path = path
        self.corrupt = corrupt

    def dialog(self, handle, **kwargs):
        assert not self.path.exists()
        assert not list(self.path.parent.glob(".ls-openpgp-*"))
        deliver = kwargs["deliver"]

        def checked(response):
            if self.corrupt is not None:
                self.corrupt(response.values)
            return deliver(response)

        kwargs["deliver"] = checked
        return super().dialog(handle, **kwargs)


@pytest.fixture
def ceremony(tmp_path, monkeypatch):
    if not envelope_io._fd_publication_available():
        pytest.skip("native trust publication requires Linux held-fd links")
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "authority.db"
    clock = [int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())]
    expires = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
    caps = frozenset({KeyCapability.CERTIFY, KeyCapability.SIGN, KeyCapability.ENCRYPT})
    inspections = {}
    for name, fingerprint in ((b"owner", "A" * 40), (b"publisher", "B" * 40),
                              (b"recovery", "C" * 40), (b"other", "D" * 40)):
        record = KeyRecord(fingerprint, "RSA", 4096, caps, date(2025, 1, 1), date(2027, 1, 1),
                           True, expires_epoch=expires, direct_capabilities=caps)
        inspections[name] = KeyInspection(record, (), _INSPECTION_SEAL)
    inspections[b"owner-alternate-bytes"] = inspections[b"owner"]
    monkeypatch.setattr(trust_state, "_clock", lambda: clock[0])
    monkeypatch.setattr(trust_state, "inspect_key", lambda *, certificate, **_: inspections[certificate])
    arguments = dict(scope=("fixture:native",), owner_certificate=b"owner",
                     publisher_certificate=b"publisher", recovery_certificates=(b"recovery",))
    return path, arguments, clock, inspections


def context_for(path, arguments, **service_options):
    descriptor = describe_trust_initialization_operation(path, **arguments)
    service = TrustDialog(path, **service_options)
    context, _ = _context(descriptor, service=service)
    return context, service


def test_persistent_confirmation_publishes_exact_roles_once(ceremony):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    result = initialize_trust_state(path, **arguments, native_input=context)
    assert load_trust_state(path, expected_scope=arguments["scope"]) == result
    assert result.owner.fingerprint == "A" * 40 and result.publisher.fingerprint == "B" * 40
    assert (result.revision, result.epoch) == (1, 1)
    assert [item.name for item in service.forms[0].fields] == ["owner", "publisher", "recovery_1"]
    assert str(path) in service.forms[0].description and "fixture:native" in service.forms[0].description
    assert service.responses[0].values == {}
    assert not list(path.parent.glob(".ls-openpgp-*"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT certificate FROM recovery_keys").fetchall() == [(b"recovery",)]
    with pytest.raises(NativeInputError):
        context.confirm_trust_initialization(context.expected)


@pytest.mark.parametrize("change", ["certificate", "roles", "scope", "recovery", "destination", "parent"])
def test_changed_selection_is_rejected_before_input(ceremony, change):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    if change == "certificate":
        arguments["owner_certificate"] = b"owner-alternate-bytes"
    elif change == "roles":
        arguments["owner_certificate"], arguments["publisher_certificate"] = b"publisher", b"owner"
    elif change == "scope":
        arguments["scope"] = ("fixture:other",)
    elif change == "recovery":
        arguments["recovery_certificates"] = (b"other",)
    elif change == "destination":
        path = path.with_name("another.db")
    else:
        path.parent.rename(path.parent.with_name("old"))
        path.parent.mkdir(mode=0o700)
    with pytest.raises(NativeInputError, match="OPERATION_MISMATCH"):
        initialize_trust_state(path, **arguments, native_input=context)
    assert not service.forms and not path.exists()


@pytest.mark.parametrize("outcome", ["cancelled", "expired", "delivery_unknown"])
def test_cancelled_or_uncertain_confirmation_does_not_initialize(ceremony, outcome):
    path, arguments, _, _ = ceremony
    context, _ = context_for(path, arguments, outcome=outcome)
    with pytest.raises(NativeInputError):
        initialize_trust_state(path, **arguments, native_input=context)
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("corruption", ["wrong", "missing", "extra"])
def test_role_confirmation_requires_exact_fields_and_values(ceremony, corruption):
    path, arguments, _, _ = ceremony

    def corrupt(values):
        if corruption == "wrong":
            values["owner"][:] = b"B" * 40
        elif corruption == "missing":
            values.pop("publisher").clear()
        else:
            values["extra"] = bytearray(b"unrequested")

    context, service = context_for(path, arguments, corrupt=corrupt)
    with pytest.raises(NativeInputError):
        initialize_trust_state(path, **arguments, native_input=context)
    assert service.responses[0].values == {}
    assert list(path.parent.iterdir()) == []


@pytest.mark.parametrize("change", ["owner", "expiry", "clock_rollback", "deadline", "destination", "executable"])
def test_wait_time_changes_deny_publication(ceremony, monkeypatch, change):
    path, arguments, clock, _ = ceremony
    context, service = context_for(path, arguments)

    def changed():
        if change == "owner":
            service.current = False
        elif change == "expiry":
            clock[0] = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        elif change == "clock_rollback":
            clock[0] -= 1
        elif change == "deadline":
            context.deadline = time.monotonic() - 1
        elif change == "destination":
            path.write_bytes(b"competitor")
        else:
            original = initialization._gpg_selection
            monkeypatch.setattr(initialization, "_gpg_selection", lambda binary: original(binary) | {"inode": -1})

    service.after_delivery = changed
    with pytest.raises((NativeInputError, trust_state.TrustStateError, TransitionError)):
        initialize_trust_state(path, **arguments, native_input=context)
    if change == "destination":
        assert path.read_bytes() == b"competitor"
    else:
        assert not path.exists()
    assert not list(path.parent.glob(".ls-openpgp-*"))


@pytest.mark.parametrize("stage", ["write", "publish", "post_publish"])
def test_publication_failure_rolls_back_only_owned_files(ceremony, monkeypatch, stage):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    if stage == "write":
        monkeypatch.setattr(initialization, "_write_database", lambda *_: (_ for _ in ()).throw(OSError("write")))
    else:
        publish = initialization.ExclusiveOutput.publish

        def failed(output, name):
            if stage == "publish":
                path.write_bytes(b"competitor")
            result = publish(output, name)
            service.current = False
            return result

        monkeypatch.setattr(initialization.ExclusiveOutput, "publish", failed)
    with pytest.raises((NativeInputError, OSError)):
        initialize_trust_state(path, **arguments, native_input=context)
    if stage == "publish":
        assert path.read_bytes() == b"competitor"
    else:
        assert not path.exists()
    assert not list(path.parent.glob(".ls-openpgp-*"))


def test_description_keeps_long_scope_and_control_characters_literal(ceremony):
    path, arguments, _, _ = ceremony
    arguments["scope"] = tuple(str(i) + "x" * 250 for i in range(8))
    path = path.with_name("authority\nname.db")
    context, service = context_for(path, arguments)
    initialize_trust_state(path, **arguments, native_input=context)
    description = service.forms[0].description
    assert all(item in description for item in arguments["scope"])
    assert "authority\\nname.db" in description and "authority\nname.db" not in description


def test_native_publication_unavailable_precedes_input(ceremony, monkeypatch):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    monkeypatch.setattr(envelope_io, "_fd_publication_available", lambda: False)
    with pytest.raises(trust_state.TrustStateError, match="native trust publication unavailable"):
        initialize_trust_state(path, **arguments, native_input=context)
    assert not service.forms and not path.exists()


def test_expiry_during_staging_removes_stage_without_publishing(ceremony, monkeypatch):
    path, arguments, clock, _ = ceremony
    context, _ = context_for(path, arguments)
    write = initialization._write_database

    def expires(fd, data):
        write(fd, data)
        clock[0] = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())

    monkeypatch.setattr(initialization, "_write_database", expires)
    with pytest.raises(TransitionError):
        initialize_trust_state(path, **arguments, native_input=context)
    assert list(path.parent.iterdir()) == []


def test_expiry_during_inspection_does_not_open_confirmation(ceremony, monkeypatch):
    path, arguments, clock, inspections = ceremony
    context, service = context_for(path, arguments)

    def expires(*, certificate, **_):
        clock[0] = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        return inspections[certificate]

    monkeypatch.setattr(trust_state, "inspect_key", expires)
    with pytest.raises(TransitionError):
        initialize_trust_state(path, **arguments, native_input=context)
    assert not service.forms and not path.exists()


def test_missing_serializer_precedes_confirmation(ceremony, monkeypatch):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    monkeypatch.setattr(initialization.sqlite3, "Connection", object)
    with pytest.raises(trust_state.TrustStateError, match="serialization unavailable"):
        initialize_trust_state(path, **arguments, native_input=context)
    assert not service.forms and not path.exists()


def test_all_eight_recovery_roles_have_independent_confirmations(ceremony):
    path, arguments, _, inspections = ceremony
    certificates = []
    for index in range(8):
        certificate = f"recovery-{index}".encode()
        prototype = inspections[b"recovery"]
        inspections[certificate] = replace(prototype, primary=replace(
            prototype.primary, fingerprint=f"{index + 10:040X}"))
        certificates.append(certificate)
    arguments["recovery_certificates"] = tuple(certificates)
    context, service = context_for(path, arguments)
    initialize_trust_state(path, **arguments, native_input=context)
    assert len(service.forms[0].fields) == 10
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT certificate FROM recovery_keys")} == set(certificates)


def test_losing_consumer_cannot_finish_the_winning_context(ceremony):
    path, arguments, _, _ = ceremony
    context, service = context_for(path, arguments)
    entered, release = Event(), Event()

    def hold_confirmation():
        entered.set()
        assert release.wait(timeout=5)

    service.after_delivery = hold_confirmation
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(initialize_trust_state, path, **arguments, native_input=context)
        try:
            assert entered.wait(timeout=5)
            with pytest.raises(NativeInputError, match="INPUT_ALREADY_USED"):
                initialize_trust_state(path, **arguments, native_input=context)
        finally:
            release.set()
        result = winner.result(timeout=5)
    assert len(service.forms) == 1
    assert load_trust_state(path, expected_scope=arguments["scope"]) == result
