"""Real signing plus offline HTTP failures for Garage browser-style uploads."""
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ls/skills/ls-garage-s3-admin/scripts/lib'))
from ls_garage import post, result_shapes, s3, validation
from ls_garage.reporting import ToolError, envelope
from test_garage_matrix import VALUES


@pytest.mark.parametrize('name', ['PresignGet', 'PresignPut', 'PresignPost'])
def test_encrypted_presigns_deliver_executable_secret_only_to_protected_file(name, tmp_path, monkeypatch):
    key = base64.b64encode(b'x' * 32).decode(); monkeypatch.setenv('GARAGE_CUSTOMER', key)
    args = {'bucket': 'example.bucket', 'key': 'opaque/../+ key', 'expires_seconds': 60,
            'encryption': {'algorithm': 'SSE-C', 'key_env': 'GARAGE_CUSTOMER'}, 'secret_output': str(tmp_path / 'secret')}
    if name == 'PresignPost': args['max_content_length'] = 100
    validation.validate('s3.' + name, args)
    output = s3.execute(name, args, VALUES)
    assert set(output) == {'secret_output', 'sensitive', 'expires_seconds'}
    assert Path(output['secret_output']).stat().st_mode & 0o777 == 0o600
    saved = json.loads(Path(output['secret_output']).read_text())
    if name == 'PresignPost':
        assert saved['fields']['x-amz-server-side-encryption-customer-key'] == key
        assert key in base64.b64decode(saved['fields']['policy']).decode()
    else:
        assert saved['headers']['x-amz-server-side-encryption-customer-key'] == key
        signed = parse_qs(urlsplit(saved['url']).query)['X-Amz-SignedHeaders'][0].split(';')
        assert {name.lower() for name in saved['headers']} <= set(signed)
    jsonschema.validate(envelope('s3.' + name, outcome='succeeded', result=output), result_shapes.schema())
    jsonschema.validate({'operation': 's3.' + name, 'args': args}, validation.request_schema())
    with pytest.raises(ToolError): validation.validate('s3.' + name, {k:v for k,v in args.items() if k != 'secret_output'})


class Reply(io.BytesIO):
    def __init__(self, status): super().__init__(b''); self.status = status


class Connection:
    instances = []
    status = 204
    failure = False
    def __init__(self, host, port, **kwargs):
        self.host, self.options, self.sent = host, kwargs, []
        self.sock = SimpleNamespace(settimeout=lambda value: None)
        self.closed = False; self.instances.append(self)
    def putrequest(self, *args): self.request = args
    def putheader(self, *args): pass
    def endheaders(self): pass
    def send(self, value):
        if self.failure: raise OSError('not confirmed')
        self.sent.append(value)
    def getresponse(self): return Reply(self.status)
    def close(self): self.closed = True


@pytest.fixture
def upload(tmp_path, monkeypatch):
    Connection.instances = []; Connection.status = 204; Connection.failure = False
    monkeypatch.setattr(post, 'HTTPSConnection', Connection)
    monkeypatch.setattr(post, 'HTTPConnection', Connection)
    source = tmp_path / 'unusual"\r\nfilename'; source.write_bytes(b'payload')
    return {'bucket': 'example.bucket', 'key': 'opaque/../\r\n+ key', 'source': str(source)}


def test_post_streams_exact_source_without_filename_header_injection(upload):
    result = s3.execute('PostObject', upload, VALUES)
    connection = Connection.instances[0]
    body = b''.join(connection.sent)
    assert b'filename="upload"' in body and b'unusual"' not in body
    assert b'payload' in body and upload['key'].encode() in body
    assert connection.closed and result['content_length'] == 7
    assert connection.options['context'].verify_mode != 0


@pytest.mark.parametrize('status,exit_name', [(403, 'service'), (307, 'service'), (502, 'uncertain')])
def test_post_never_replays_service_or_redirect_responses(upload, status, exit_name):
    Connection.status = status
    with pytest.raises(ToolError) as error: s3.execute('PostObject', upload, VALUES)
    assert error.value.exit_name == exit_name
    assert len(Connection.instances) == 1 and Connection.instances[0].closed


def test_post_ambiguous_disconnect_is_unknown_once(upload):
    Connection.failure = True
    with pytest.raises(ToolError) as error: s3.execute('PostObject', upload, VALUES)
    assert error.value.exit_name == 'uncertain'
    assert len(Connection.instances) == 1


def test_post_rejects_signed_form_to_a_different_endpoint_before_connect(upload, monkeypatch):
    class Wrong:
        def generate_presigned_post(self, *args, **kwargs): return {'url': 'https://other.example/bucket', 'fields': {'key': 'key'}}
    monkeypatch.setattr(s3, '_client', lambda values: Wrong())
    with pytest.raises(ToolError) as error: s3.execute('PostObject', upload, VALUES)
    assert error.value.code == 'unsafe_endpoint' and Connection.instances == []
