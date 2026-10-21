"""Direct multipart parts share streaming, size and identity guarantees."""
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_SKILLS = {
    'backblaze': 'ls-backblaze-b2-storage',
    'garage': 'ls-garage-s3-admin',
}


@pytest.fixture(params=['backblaze', 'garage'])
def adapter(request, monkeypatch):
    monkeypatch.syspath_prepend(
        str(ROOT / 'ls/skills' / PROVIDER_SKILLS[request.param] / 'scripts/lib')
    )
    return importlib.import_module('ls_' + request.param + '.s3')


def args(path):
    return {'bucket': 'example.bucket', 'key': 'opaque/../key', 'upload_id': 'id', 'part_number': 1, 'source': str(path)}


def test_oversized_sparse_part_rejects_before_dispatch(adapter, monkeypatch, tmp_path):
    source = tmp_path / 'sparse'
    with source.open('wb') as stream: stream.truncate(5 * 1024**3 + 1)
    class Client:
        def upload_part(self, **kwargs): raise AssertionError('oversized source dispatched')
    monkeypatch.setattr(adapter, '_client', lambda values: Client())
    with pytest.raises(adapter.ToolError) as error: adapter.execute('UploadPart', args(source), {})
    assert error.value.exit_name == 'input'


def test_part_body_bounded_and_length_explicit(adapter, monkeypatch, tmp_path):
    source = tmp_path / 'source'; source.write_bytes(b'x' * (2 * 1024**2 + 1))
    sizes = []
    class Client:
        def upload_part(self, **kwargs):
            while block := kwargs['Body'].read(100 * 1024**2): sizes.append(len(block))
            assert sum(sizes) == kwargs['ContentLength'] == source.stat().st_size
            return {'ETag': 'part', 'ResponseMetadata': {}}
    monkeypatch.setattr(adapter, '_client', lambda values: Client())
    assert adapter.execute('UploadPart', args(source), {})['ETag'] == 'part'
    assert max(sizes) <= 1024**2


def test_changed_part_source_never_reports_success(adapter, monkeypatch, tmp_path):
    source = tmp_path / 'source'; source.write_bytes(b'old')
    class Client:
        def upload_part(self, **kwargs):
            source.write_bytes(b'different-length')
            return {'ETag': 'part', 'ResponseMetadata': {}}
    monkeypatch.setattr(adapter, '_client', lambda values: Client())
    result = adapter.execute('UploadPart', args(source), {})
    assert result['partial'] is True and result['etag'] == 'part'
    assert result['upload_id'] == 'id' and 'ListParts' in result['reconciliation']
