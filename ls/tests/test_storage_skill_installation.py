"""Exercise real materialized skill entrypoints with no source-checkout imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest

from ls.core.reference_materializer import materialize_package_artifact

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ("backblaze", "garage")
HARNESS = """
import runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('network attempted during offline acceptance')
socket.create_connection = forbidden
socket.socket.connect = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
STUB_HARNESS = """
import boto3.session
from botocore.stub import Stubber
original = boto3.session.Session.client
def client(self, *args, **kwargs):
    value = original(self, *args, **kwargs)
    stub = Stubber(value)
    stub.add_response('list_buckets', {'Buckets': []}, {})
    stub.activate()
    return value
boto3.session.Session.client = client
""" + HARNESS


@pytest.fixture(params=PROVIDERS)
def installed_skill(request, tmp_path):
    provider = request.param
    package = f"ls-{provider}"
    destination = tmp_path / "installed" / package
    receipt = materialize_package_artifact(
        ROOT, ROOT / "ls/skills" / package, destination,
        package_name=package, package_type="skill", emitter="storage-installation-test",
    )
    assert receipt["validation"]["ok"]
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    return provider, destination, elsewhere


def execute(installed, arguments, *, python=sys.executable, sdk=False, stub=False):
    provider, directory, elsewhere = installed
    command = [str(python), "-I"]
    if not sdk:
        command.append("-S")
    command += ["-c", STUB_HARNESS if stub else HARNESS,
                str(directory / f"scripts/{provider}.py"), *arguments]
    env = {"PATH": os.environ["PATH"], "HOME": str(elsewhere),
           "STORAGE_TEST_ID": "offline-access-id", "STORAGE_TEST_SECRET": "offline-secret-sentinel"}
    result = subprocess.run(command, cwd=elsewhere, env=env, capture_output=True, text=True, timeout=30)
    assert "offline-secret-sentinel" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    return result


def configuration(installed):
    provider, _, elsewhere = installed
    endpoint = ("https://s3.us-west-004.backblazeb2.com" if provider == "backblaze"
                else "https://storage.example.test")
    path = elsewhere / "configuration.json"
    path.write_text(json.dumps({"s3": {"endpoint": endpoint, "region": "us-west-004",
        "access_key_id": {"env": "STORAGE_TEST_ID"},
        "secret_access_key": {"env": "STORAGE_TEST_SECRET"}}}))
    return path


def test_materialized_offline_interfaces_need_neither_sdk_nor_checkout(installed_skill):
    for arguments in (["--help"], ["--capabilities"], ["--schema", "request"], ["--schema", "result"]):
        response = execute(installed_skill, arguments)
        assert response.returncode == 0, response.stdout + response.stderr
        if arguments[0] == "--schema":
            schema = json.loads(response.stdout)
            assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
            jsonschema.Draft202012Validator.check_schema(schema)
    response = execute(installed_skill, ["--tool", "s3.ListBuckets", "--args-json", "{}"])
    assert response.returncode == 0, response.stdout + response.stderr
    assert json.loads(response.stdout)["outcome"] == "planned"


def test_materialized_missing_sdk_fails_explicitly_without_network(installed_skill):
    response = execute(installed_skill, ["--tool", "s3.ListBuckets", "--args-json", "{}",
                                       "--config", str(configuration(installed_skill)), "--apply"])
    assert response.returncode == 2, response.stdout + response.stderr
    assert json.loads(response.stdout)["error"]["code"] == "dependency_missing"


@pytest.fixture(scope="session")
def isolated_sdk_python(tmp_path_factory):
    directory = tmp_path_factory.mktemp("storage-sdk-runtime")
    subprocess.run(["uv", "venv", "--python", sys.executable, str(directory)],
                   check=True, capture_output=True, timeout=60)
    python = directory / "bin/python"
    subprocess.run(["uv", "pip", "install", "--python", str(python),
                    "--require-hashes", "-r", str(ROOT / "ls/skills/ls-backblaze/requirements-s3-sdk.txt")],
                   check=True, capture_output=True, timeout=60)
    return python


def test_materialized_sdk_call_uses_hashed_independent_environment(installed_skill, isolated_sdk_python):
    response = execute(installed_skill, ["--tool", "s3.ListBuckets", "--args-json", "{}",
                                       "--config", str(configuration(installed_skill)), "--apply"],
                       python=isolated_sdk_python, sdk=True, stub=True)
    assert response.returncode == 0, response.stdout + response.stderr
    assert json.loads(response.stdout)["outcome"] == "succeeded"
