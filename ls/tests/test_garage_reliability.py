"""Reconciliation, deadline and durable-secret regressions at failure boundaries."""
import io
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ls/skills/ls-garage-s3-admin/scripts/lib'))
from ls_garage import admin, http_transport, s3, secret_files, transfers, validation
from ls_garage.reporting import ToolError
from ls_garage.s3_requests import Requests
from test_garage_matrix import ADMIN_VALUES, VALUES, Response, sample
from test_garage_transfer_recovery import Multipart, upload_args


def test_safe_admin_retry_after_is_capped_and_shared_with_deadline(monkeypatch):
    now, sleeps, calls = [0], [], []
    monkeypatch.setattr(http_transport.time, 'monotonic', lambda: now[0])
    def sleep(delay): sleeps.append(delay); now[0] += delay
    monkeypatch.setattr(http_transport.time, 'sleep', sleep)
    class Opener:
        def open(self, request, timeout):
            calls.append(timeout)
            if len(calls) < 3: raise HTTPError(request.full_url, 429, 'slow', {'Retry-After': '999'}, None)
            return Response(request.full_url, [])
    monkeypatch.setattr(admin, 'build_opener', lambda *args: Opener())
    assert admin.execute('ListBuckets', {}, {**ADMIN_VALUES, 'request_budget_seconds': 65}) == []
    assert sleeps == [30, 30] and calls == [5, 5, 5]


def test_admin_response_reader_applies_read_timeout_and_stops_at_deadline(monkeypatch):
    now, timeouts = [0], []
    monkeypatch.setattr(http_transport.time, 'monotonic', lambda: now[0])
    class Socket:
        def settimeout(self, value): timeouts.append(value)
    class Response:
        fp = type('FP', (), {'raw': type('Raw', (), {'_sock': Socket()})()})()
        def read(self, size): now[0] += 61; return b'{}'
    with pytest.raises(ToolError) as error: http_transport.read(Response(), 60)
    assert error.value.code == 'request_budget_expired' and timeouts == [60]


def test_s3_safe_retry_does_not_allow_mutation_replay(monkeypatch):
    calls = []
    class Client:
        def list_buckets(self, **args):
            calls.append('read')
            if len(calls) == 1: raise OSError('reset')
            return {'Buckets': []}
        def delete_bucket(self, **args): calls.append('write'); raise OSError('lost response')
    monkeypatch.setattr(s3.time, 'sleep', lambda delay: None)
    client = Requests(Client(), s3.time.monotonic() + 300)
    assert client.list_buckets() == {'Buckets': []}
    with pytest.raises(ToolError) as error: client.delete_bucket(Bucket='bucket')
    assert error.value.exit_name == 'uncertain' and calls == ['read', 'read', 'write']


def test_bounded_list_returns_effective_marker_and_repeated_tokens_reject(monkeypatch):
    class Client:
        def list_objects(self, **args): return {'IsTruncated': True, 'Contents': [{'Key': 'opaque-next'}]}
    monkeypatch.setattr(s3, '_client', lambda values: Client())
    result = s3.execute('ListObjects', {'bucket': 'example.bucket', 'max_pages': 1}, VALUES)
    assert result['truncated'] and result['next_continuation'] == 'opaque-next'
    with pytest.raises(ToolError) as error: s3.execute('ListObjects', {'bucket': 'example.bucket', 'max_pages': 3}, VALUES)
    assert error.value.code == 'repeated_continuation'


def test_reserved_secret_handles_short_writes_and_rejects_replaced_path(tmp_path, monkeypatch):
    path = tmp_path / 'secret'; reserved = secret_files.reserve(str(path))
    original = secret_files.os.write
    monkeypatch.setattr(secret_files.os, 'write', lambda fd, data: original(fd, data[:2]))
    try: secret_files.deliver_reserved(reserved, {'secretAccessKey': 'never-print'})
    finally: os.close(reserved[1])
    assert json.loads(path.read_text()) == {'secretAccessKey': 'never-print'}
    second = tmp_path / 'second'; reserved = secret_files.reserve(str(second)); second.unlink(); second.write_text('preserve')
    try:
        with pytest.raises(ToolError): secret_files.deliver_reserved(reserved, {'secret': 'must-not-write'})
        assert second.read_text() == 'preserve'
    finally: os.close(reserved[1])


def test_create_key_delivery_failure_reports_id_without_recreate(monkeypatch, tmp_path):
    from ls_garage import admin_schema
    output = sample(admin_schema.result_schema('CreateKey'), {'$defs': admin_schema.definitions()})
    output.update(accessKeyId='created-id', secretAccessKey='never-print', name='name')
    calls = []
    class Opener:
        def open(self, request, timeout): calls.append(request); return Response(request.full_url, output)
    monkeypatch.setattr(admin, 'build_opener', lambda *args: Opener())
    def failure(*args): raise ToolError('delivery_failed', 'not delivered', 'uncertain')
    monkeypatch.setattr(admin, 'deliver_reserved', failure)
    result = admin.execute('CreateKey', {'name': 'name', 'secret_output': str(tmp_path / 'secret')}, ADMIN_VALUES)
    assert result['partial'] and result['access_key_id'] == 'created-id' and len(calls) == 1
    assert 'never-print' not in json.dumps(result)


def test_multipart_source_mismatch_and_cancellation_preserve_recovery(tmp_path):
    args = upload_args(tmp_path)
    class Cancelled(Multipart):
        def upload_part(self, **args): raise KeyboardInterrupt()
    client = Cancelled()
    with pytest.raises(KeyboardInterrupt): transfers.upload(client, args, 'account')
    assert not Path(args['checkpoint'] + '.lock').exists()
    assert json.loads(Path(args['checkpoint']).read_text())['phase'] == 'uploading'
    Path(args['source']).write_bytes(b'different')
    calls = client.calls[:]
    with pytest.raises(ToolError): transfers.upload(client, args, 'account', resume=True)
    assert client.calls == calls


def test_multipart_identity_includes_endpoint_and_region(monkeypatch):
    class Client: pass
    identities = []
    monkeypatch.setattr(s3, '_client', lambda values: Client())
    monkeypatch.setattr(s3, 'resumable_upload', lambda client, args, marker, **kwargs: identities.append(marker) or {})
    for changed in ({}, {'endpoint_url': 'https://second.example'}, {'region_name': 'another'}):
        s3.execute('UploadFileMultipart', {}, {**VALUES, **changed})
    assert len(set(identities)) == 3


@pytest.mark.parametrize('args', [{'id': 'id', 'name': None}, {'id': 'id', 'name': 'valid', 'neverExpires': False}])
def test_ignored_key_update_values_reject_locally(args):
    with pytest.raises(ToolError): validation.validate('admin.UpdateKey', args)
