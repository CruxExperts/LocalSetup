"""The installed Garage contract contains only the reviewed administration slice."""
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from ls.core.s3_sdk import garage_schema

ROOT = Path(__file__).resolve().parents[2]


def test_installed_schema_is_closed_over_allowlisted_references():
    document = json.loads((ROOT / 'ls/skills/ls-garage-s3-admin/references/admin-api-v2.schema.json').read_text())
    assert document['info']['version'] == 'v2.3.0'
    assert document['x-source']['sha256'] == garage_schema.SOURCE_SHA256
    operations = {op['operationId'] for methods in document['paths'].values() for op in methods.values()}
    assert operations == garage_schema.OPERATIONS
    def references(value):
        if isinstance(value, dict):
            if '$ref' in value:
                assert value['$ref'].startswith('#/components/schemas/')
                assert value['$ref'].split('/')[-1] in document['components']['schemas']
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
    references(document)


def test_unreviewed_source_fails_before_destination_write(tmp_path):
    source = tmp_path / 'unreviewed.json'
    source.write_text('{}')
    with pytest.raises(ValueError, match='hash differs'):
        garage_schema.generate(tmp_path, source)
    assert not (tmp_path / 'ls').exists()


def test_extraction_preserves_constraints_not_unrelated_operations(monkeypatch):
    fixture = {'openapi': '3.1.0', 'info': {'version': 'v2.3.0'}, 'paths': {
        '/bucket': {'get': {'operationId': 'ListBuckets', 'responses': {'200': {'$ref': '#/components/schemas/A'}}}},
        '/repair': {'post': {'operationId': 'Repair', 'responses': {}}}},
        'components': {'schemas': {'A': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'string', 'minLength': 1}}, 'description': 'prose'}, 'Unused': {'type': 'string'}}}}
    raw = json.dumps(fixture).encode()
    monkeypatch.setattr(garage_schema, 'SOURCE_SHA256', hashlib.sha256(raw).hexdigest())
    result = json.loads(garage_schema.extract(raw))
    assert set(result['paths']) == {'/bucket'}
    assert set(result['components']['schemas']) == {'A'}
    assert result['components']['schemas']['A']['required'] == ['id']
    assert result['components']['schemas']['A']['properties']['id']['minLength'] == 1
    assert 'description' not in result['components']['schemas']['A']


def test_preserved_licensed_source_reconstructs_installed_projection():
    base = ROOT / 'ls/skills/ls-garage-s3-admin/references'
    raw = gzip.decompress((base / 'upstream-admin-api-v2.json.gz').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == garage_schema.SOURCE_SHA256
    assert garage_schema.extract(raw) == (base / 'admin-api-v2.schema.json').read_bytes()
