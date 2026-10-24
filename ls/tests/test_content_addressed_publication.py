"""Exact delivery and no-replay recovery on nonconditional carriers."""
import hashlib

import pytest

from ls.core.openpgp import container_transfer as transfer


class Carrier:
    def __init__(self, *, retain=True, lose_ack=False):
        self.data = {}
        self.calls = 0
        self.retain, self.lose_ack = retain, lose_ack
        self.visible = True

    def put(self, key, stream, *, length, sha256, timeout_seconds, cancel_check):
        assert timeout_seconds > 0 and not cancel_check()
        self.calls += 1
        data = b''.join(iter(lambda: stream.read(1024), b''))
        assert (len(data), hashlib.sha256(data).hexdigest()) == (length, sha256)
        if self.retain:
            self.data[key] = data
        if self.lose_ack:
            raise OSError('synthetic lost acknowledgement')

    def observe(self, key, *, timeout_seconds, cancel_check):
        assert timeout_seconds > 0 and not cancel_check()
        data = self.data.get(key) if self.visible else None
        return None if data is None else (len(data), hashlib.sha256(data).hexdigest())


def publish(tmp_path, carrier, data=b'opaque ciphertext', **kwargs):
    source = tmp_path / 'volume.lssc'
    source.write_bytes(data)
    source.chmod(0o600)
    return transfer._publish_file_exact(
        source, destination=carrier, key='canonical', length=len(data),
        sha256=hashlib.sha256(data).hexdigest(), refresh=lambda: None,
        publication_mode='content_addressed', **kwargs,
    )


def test_distinct_ciphertexts_have_distinct_locations_and_duplicates_do_not_write(tmp_path):
    carrier = Carrier()
    first = publish(tmp_path, carrier, b'first ciphertext')
    second = publish(tmp_path, carrier, b'second ciphertext')
    assert first.key == second.key == 'canonical'
    assert first.locator_key != second.locator_key
    assert set(carrier.data.values()) == {b'first ciphertext', b'second ciphertext'}
    assert first.status == second.status == 'verified'
    assert publish(tmp_path, carrier, b'first ciphertext').status == 'identical'
    assert carrier.calls == 2


@pytest.mark.parametrize('retain,lose_ack', [(True, False), (True, True), (False, False), (False, True)])
def test_readback_not_acknowledgement_proves_delivery(tmp_path, retain, lose_ack):
    carrier = Carrier(retain=retain, lose_ack=lose_ack)
    if retain:
        result = publish(tmp_path, carrier)
        assert result.status == ('observed_after_uncertain' if lose_ack else 'verified')
    else:
        with pytest.raises(transfer.TransferError, match='publish_uncertain'):
            publish(tmp_path, carrier)
    assert carrier.calls == 1


def test_partition_recovery_only_observes_never_replays(tmp_path):
    carrier = Carrier(lose_ack=True)
    carrier.visible = False
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        publish(tmp_path, carrier)
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        publish(tmp_path, carrier, reconcile_only=True)
    carrier.visible = True
    # A reconciler needs no write capability.
    carrier.put = None
    assert publish(tmp_path, carrier, reconcile_only=True).status == 'identical'
    assert carrier.calls == 1


def test_collision_and_observation_failure_never_dispatch(tmp_path):
    carrier = Carrier()
    key = 'canonical.sha256-' + hashlib.sha256(b'opaque ciphertext').hexdigest()
    carrier.data[key] = b'incorrect bytes'
    with pytest.raises(transfer.TransferError, match='ciphertext_collision'):
        publish(tmp_path, carrier)

    def unavailable(*_args, **_kwargs):
        raise OSError('partitioned')

    carrier.observe = unavailable
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        publish(tmp_path, carrier)
    assert carrier.calls == 0


@pytest.mark.parametrize('failure', ['cancel', 'deadline', 'corrupt'])
def test_post_dispatch_failures_preserve_uncertainty_and_retained_bytes(tmp_path, monkeypatch, failure):
    carrier, cancelled, clock = Carrier(), [False], [0.0]
    monkeypatch.setattr(transfer.time, 'monotonic', lambda: clock[0])
    original = carrier.put

    def late_failure(key, *args, **kwargs):
        original(key, *args, **kwargs)
        if failure == 'cancel':
            cancelled[0] = True
        elif failure == 'deadline':
            clock[0] = 2.0
        else:
            carrier.data[key] = b'different retained bytes'

    carrier.put = late_failure
    expected = 'ciphertext_collision' if failure == 'corrupt' else 'publish_uncertain'
    with pytest.raises(transfer.TransferError, match=expected):
        publish(tmp_path, carrier, deadline=1, cancel_check=lambda: cancelled[0])
    assert carrier.calls == 1 and len(carrier.data) == 1


@pytest.mark.parametrize('mode', ['conditional', 'content_addressed'])
def test_reconciliation_absent_is_read_only_for_each_mode(tmp_path, mode):
    carrier = Carrier()
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        transfer._publish_file_exact(tmp_path / 'unused', destination=carrier,
            key='canonical', length=3, sha256=hashlib.sha256(b'abc').hexdigest(),
            refresh=lambda: None, publication_mode=mode, reconcile_only=True)
    assert carrier.calls == 0 and not list(tmp_path.iterdir())


def test_mode_validation_is_safe_and_local_reconciliation_does_not_create(tmp_path):
    source = tmp_path / 'unused'
    args = dict(destination=tmp_path / 'absent', key='canonical', length=3,
                sha256=hashlib.sha256(b'abc').hexdigest(), refresh=lambda: None)
    with pytest.raises(transfer.TransferError, match='publication_invalid'):
        transfer._publish_file_exact(source, publication_mode=[], **args)
    with pytest.raises(transfer.TransferError, match='publish_uncertain'):
        transfer._publish_file_exact(source, reconcile_only=True, **args)
    assert not list(tmp_path.iterdir())


def test_reconciliation_checks_existing_conditional_bytes_without_create_capability(tmp_path):
    data = b'exact'
    carrier = Carrier()
    carrier.data['canonical'] = data
    args = dict(key='canonical', length=len(data), sha256=hashlib.sha256(data).hexdigest(),
                refresh=lambda: None, reconcile_only=True)
    assert transfer._publish_file_exact(tmp_path / 'unused', destination=carrier, **args).status == 'identical'
    target = tmp_path / 'existing'
    target.write_bytes(data)
    target.chmod(0o600)
    assert transfer._publish_file_exact(tmp_path / 'unused', destination=target, **args).status == 'identical'
    target.write_bytes(b'wrong')
    with pytest.raises(transfer.TransferError, match='ciphertext_collision'):
        transfer._publish_file_exact(tmp_path / 'unused', destination=target, **args)
    assert carrier.calls == 0
