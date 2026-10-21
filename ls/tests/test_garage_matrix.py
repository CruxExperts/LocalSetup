"""All allowlisted operations must validate and satisfy their offline wire contracts."""
from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import jsonschema
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ls/skills/ls-garage-s3-admin/scripts/lib'))
from ls_garage import admin, admin_schema, cli, result_shapes, s3, storage_shapes, validation
from ls_garage.reporting import ToolError, envelope
from test_backblaze_matrix import S3_ARGS as B2_ARGS

S3_ARGS = {name: {k: copy.deepcopy(v) for k,v in B2_ARGS[name].items() if k in fields}
           for name, fields in storage_shapes.S3.items() if name in B2_ARGS}
S3_ARGS['DeleteObjects']['objects'] = [{'key': 'k'}]
S3_ARGS.update({name: {'bucket': 'example.bucket'} for name in ['GetBucketWebsite', 'DeleteBucketWebsite', 'GetBucketLifecycleConfiguration', 'DeleteBucketLifecycle']})
S3_ARGS.update({'PostObject': {'bucket': 'example.bucket', 'key': 'k', 'source': 'source'},
 'PresignPost': {'bucket': 'example.bucket', 'key': 'k', 'expires_seconds': 60, 'max_content_length': 100},
 'PutBucketWebsite': {'bucket': 'example.bucket', 'website': {'IndexDocument': {'Suffix': 'index.html'}, 'ErrorDocument': {'Key': '404.html'}}},
 'PutBucketLifecycleConfiguration': {'bucket': 'example.bucket', 'rules': [{'Status': 'Enabled', 'Filter': {'Prefix': 'old/'}, 'Expiration': {'Days': 30}}]}})
ADMIN_ARGS = {
 'ListBuckets': {}, 'GetBucketInfo': {'id': 'bucket-id'}, 'CreateBucket': {'globalAlias': 'example.bucket'},
 'DeleteBucket': {'id': 'bucket-id'}, 'UpdateBucket': {'id': 'bucket-id', 'quotas': {'maxSize': 1024, 'maxObjects': None}, 'corsRules': [], 'lifecycleRules': [], 'websiteAccess': {'enabled': False}},
 'ListKeys': {}, 'GetKeyInfo': {'id': 'key-id'}, 'CreateKey': {'name': 'created', 'secret_output': 'secret-output', 'allow': {'createBucket': True}},
 'UpdateKey': {'id': 'key-id', 'name': 'new-name', 'neverExpires': True}, 'ImportKey': {'restore_existing': True, 'restore_file': {'file': 'restore-file'}}, 'DeleteKey': {'id': 'key-id'},
 'AllowBucketKey': {'bucketId': 'bucket-id', 'accessKeyId': 'key-id', 'permissions': {'read': True, 'write': True}},
 'DenyBucketKey': {'bucketId': 'bucket-id', 'accessKeyId': 'key-id', 'permissions': {'owner': True}},
 'AddBucketAlias': {'bucketId': 'bucket-id', 'globalAlias': 'example.bucket'},
 'RemoveBucketAlias': {'bucketId': 'bucket-id', 'accessKeyId': 'key-id', 'localAlias': 'example.bucket'}, 'GetClusterHealth': {},
}
VALUES = {'endpoint_url': 'https://garage.example.test', 'region_name': 'garage', 'aws_access_key_id': 'id', 'aws_secret_access_key': 'secret', 'verify': True}
ADMIN_VALUES = {'endpoint': 'https://admin.example.test', 'token': 'secret-token'}


def assert_schema(value, schema):
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(value))
    assert not errors, [(list(error.path), error.validator) for error in errors]


def sample(shape, root):
    if '$ref' in shape:
        selected = root
        for part in shape['$ref'][2:].split('/'):
            selected = selected[part]
        return sample(selected, root)
    if 'const' in shape: return shape['const']
    if 'enum' in shape: return shape['enum'][0]
    if 'oneOf' in shape: return sample(shape['oneOf'][0], root)
    kind = shape.get('type', 'object')
    if isinstance(kind, list): kind = kind[0]
    if kind == 'object': return {key: sample(shape['properties'][key], root) for key in shape.get('required', [])}
    if kind == 'array': return []
    if kind == 'boolean': return False
    if kind in {'integer', 'number'}: return max(0, shape.get('minimum', 0))
    if kind == 'null': return None
    return '2026-09-08T00:00:00Z' if shape.get('format') == 'date-time' else 'example-value'


def arguments(namespace, name, tmp_path):
    args = copy.deepcopy((S3_ARGS if namespace == 's3' else ADMIN_ARGS)[name])
    for field in {'source', 'destination', 'checkpoint', 'secret_output'} & set(args):
        args[field] = str(tmp_path / field)
        if field == 'source': Path(args[field]).write_bytes(b'abc')
    if name == 'ImportKey':
        path = tmp_path / 'restore.json'
        path.write_text(json.dumps({'accessKeyId': 'key-id', 'secretAccessKey': 'exported-secret', 'name': 'restored'})); path.chmod(0o600)
        args['restore_file'] = {'file': str(path)}
    return args


def test_exact_registry_coverage():
    assert set(S3_ARGS) == set(storage_shapes.S3)
    assert set(ADMIN_ARGS) == set(admin_schema.OPERATIONS)


@pytest.mark.parametrize('namespace,name', [('s3', name) for name in S3_ARGS] + [('admin', name) for name in ADMIN_ARGS])
def test_every_request_closed_and_policy_classified(namespace, name, tmp_path):
    args = arguments(namespace, name, tmp_path)
    operation = namespace + '.' + name
    validation.validate(operation, args)
    assert_schema({'operation': operation, 'args': args}, validation.request_schema())
    with pytest.raises(ToolError): validation.validate(operation, {**args, 'unsupported': True})
    schema = storage_shapes.args_schema(name) if namespace == 's3' else admin_schema.args_schema(name)
    for required in schema.get('required', []):
        invalid = {key: value for key, value in args.items() if key != required}
        with pytest.raises(ToolError): validation.validate(operation, invalid)
    parsed = cli.parser().parse_args([])
    gated = operation in validation.DESTRUCTIVE or name in validation.ACCESS or name in validation.OVERWRITE
    if gated:
        with pytest.raises(ToolError) as error: cli._safety(operation, name, args, parsed)
        assert error.value.exit_name == 'policy'
    else: cli._safety(operation, name, args, parsed)


class Response(io.BytesIO):
    def __init__(self, url, value):
        super().__init__(json.dumps(value).encode() if value is not None else b'')
        self.url = url
    def geturl(self): return self.url


@pytest.mark.parametrize('name', sorted(ADMIN_ARGS))
@pytest.mark.parametrize('rejected', [False, True])
def test_every_admin_operation_wire_and_result(name, rejected, tmp_path, monkeypatch):
    args = arguments('admin', name, tmp_path)
    op = admin_schema.operation(name)
    document = admin_schema.source()
    output_shape = op['responses']['200'].get('content', {}).get('application/json', {}).get('schema')
    output = sample(output_shape, document) if output_shape else None
    if name == 'CreateKey': output.update(accessKeyId='created-id', secretAccessKey='created-secret', name='created')
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            assert request.get_method() == op['method']
            assert request.full_url.split('?')[0] == ADMIN_VALUES['endpoint'] + op['path']
            if 'requestBody' in op:
                wire_schema = {**op['requestBody']['content']['application/json']['schema'], 'components': document['components']}
                assert_schema(json.loads(request.data), wire_schema)
            if rejected: raise HTTPError(request.full_url, 403, 'denied', {}, None)
            return Response(request.full_url, output)
    monkeypatch.setattr(admin, 'build_opener', lambda *a: Opener())
    if rejected:
        with pytest.raises(ToolError) as error: admin.execute(name, args, ADMIN_VALUES)
        assert error.value.exit_name == 'service'
        assert_schema(error.value.as_envelope('admin.' + name), result_shapes.schema())
    else:
        result = admin.execute(name, args, ADMIN_VALUES)
        assert_schema(envelope('admin.' + name, outcome='succeeded', result=result), result_shapes.schema())
        assert 'created-secret' not in json.dumps(result)
    assert len(seen) == 1


HELPERS = {'PostObject', 'PresignGet', 'PresignPut', 'PresignPost', 'UploadFileMultipart', 'ResumeMultipartUpload'}
@pytest.mark.parametrize('name', sorted(set(S3_ARGS) - HELPERS))
@pytest.mark.parametrize('rejected', [False, True])
def test_every_direct_s3_operation_real_sdk_serialization(name, rejected, tmp_path, monkeypatch):
    args = arguments('s3', name, tmp_path)
    client = s3._client(VALUES)
    method = ''.join('_' + char.lower() if char.isupper() else char for char in name).lstrip('_')
    response = {'ResponseMetadata': {'HTTPStatusCode': 200}, **sample(s3.s3_response.__globals__['s3_shapes']()[name], {})}
    if name == 'GetObject': response.update(Body=StreamingBody(io.BytesIO(b'abc'), 3), ContentLength=3)
    if name == 'DeleteObjects': response['Deleted'] = [{'Key': 'k'}]
    if name in {'CopyObject', 'UploadPartCopy'}: response['CopyObjectResult' if name == 'CopyObject' else 'CopyPartResult'] = {'ETag': 'etag'}
    with Stubber(client) as stub:
        if rejected: stub.add_client_error(method, 'AccessDenied', http_status_code=403)
        else: stub.add_response(method, response)
        monkeypatch.setattr(s3, '_client', lambda values: client)
        if rejected:
            with pytest.raises(ToolError) as error: s3.execute(name, args, VALUES)
            assert error.value.exit_name == 'service'
        else:
            result = s3.execute(name, args, VALUES)
            assert_schema(envelope('s3.' + name, outcome='succeeded', result=result), result_shapes.schema())
        stub.assert_no_pending_responses()
