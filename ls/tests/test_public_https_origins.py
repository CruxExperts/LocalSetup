"""Network-free checks for optional public HTTPS origin helpers."""

import asyncio
import importlib.util
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/ls-public-https-origins/scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location("https_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hello(name=None):
    extension = b""
    if name is not None:
        name = name.encode("ascii")
        server_name = b"\x00" + len(name).to_bytes(2, "big") + name
        names = len(server_name).to_bytes(2, "big") + server_name
        extension = b"\x00\x00" + len(names).to_bytes(2, "big") + names
    body = (b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01"
            + b"\x01\x00" + len(extension).to_bytes(2, "big") + extension)
    return b"\x01" + len(body).to_bytes(3, "big") + body


def record(payload):
    return b"\x16\x03\x03" + len(payload).to_bytes(2, "big") + payload


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1",
                                  "https://storage.local", "https://user:secret@example.com",
                                  "https://example.com:444", "https://example.com/?token=secret"])
def test_endpoint_rejects_nonpublic_or_sensitive_forms(url):
    with pytest.raises(ValueError) as error:
        load_script("public_https").endpoint(url)
    assert "secret" not in str(error.value)
    assert "token" not in str(error.value)


def test_endpoint_accepts_public_hostname_and_opt_in_path():
    endpoint = load_script("public_https").endpoint
    assert endpoint("https://Storage.Example.com:443/") == "storage.example.com"
    with pytest.raises(ValueError):
        endpoint("https://storage.example.com/bucket")
    assert endpoint("https://storage.example.com/bucket", allow_path=True) == "storage.example.com"


def test_fragmented_client_hello_and_exact_sni():
    gateway = load_script("tls_gateway")
    message = hello("storage.example.com")

    async def read_fragmented_hello():
        reader = asyncio.StreamReader()
        reader.feed_data(record(message[:19]) + record(message[19:]))
        reader.feed_eof()
        return await gateway.read_client_hello(reader)

    name, original = asyncio.run(read_fragmented_hello())
    assert name == "storage.example.com"
    assert original == record(message[:19]) + record(message[19:])


def test_gateway_rejects_mismatched_sni_before_any_upstream_connection(monkeypatch):
    gateway = load_script("tls_gateway")

    class ClientWriter:
        closed = False

        def get_extra_info(self, name):
            return ("10.66.0.5", 1234) if name == "peername" else None

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Server:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def serve_forever(self):
            reader = asyncio.StreamReader()
            reader.feed_data(record(hello("other.example.com")))
            reader.feed_eof()
            await handler(reader, writer)

    async def start_server(callback, *_args, **_kwargs):
        nonlocal handler
        handler = callback
        return Server()

    async def forbidden_lookup(_name):
        raise AssertionError("mismatched SNI reached DNS")

    handler = None
    writer = ClientWriter()
    monkeypatch.setattr(gateway.asyncio, "start_server", start_server)
    monkeypatch.setattr(gateway, "public_address", forbidden_lookup)
    asyncio.run(gateway.serve({"storage.example.com"}, "10.66.0.2", "10.66.0.0/24", 443))
    assert writer.closed


@pytest.mark.parametrize("message", [hello(), hello("storage.example.com")[:-1], b"not TLS"])
def test_missing_or_malformed_sni_fails_closed(message):
    with pytest.raises(ValueError):
        load_script("tls_gateway").client_hello_name(message)


def test_nonpublic_dns_answer_rejected_without_connecting(monkeypatch):
    gateway = load_script("tls_gateway")

    async def resolve(*_args, **_kwargs):
        return [(None, None, None, None, ("198.51.100.7", 443))]

    async def check():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        with pytest.raises(ValueError, match="public addresses"):
            await gateway.public_address("storage.example.com")

    asyncio.run(check())


def test_socket_bridge_rejects_unexpected_target_and_replaces_owned_socket(tmp_path, monkeypatch):
    bridge = load_script("socket_bridge")
    source = tmp_path / "origin.sock"
    target_dir = tmp_path / "proxy"
    target_dir.mkdir(mode=0o700)
    target = target_dir / "origin.sock"
    owner = os.getuid()
    original_lstat = Path.lstat

    def root_owned_target_dir(path):
        result = original_lstat(path)
        if path == target_dir:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=0, st_dev=result.st_dev)
        return result

    monkeypatch.setattr(Path, "lstat", root_owned_target_dir)
    monkeypatch.setattr(bridge.os, "chown", lambda *_args, **_kwargs: None)
    first = socket.socket(socket.AF_UNIX)
    second = socket.socket(socket.AF_UNIX)
    try:
        first.bind(str(source))
        source.chmod(0o600)
        target.write_text("do not replace")
        with pytest.raises(ValueError, match="unexpected destination"):
            bridge.link_socket(source, target, owner)
        assert target.read_text() == "do not replace"
        target.unlink()
        bridge.link_socket(source, target, owner)
        old_inode = target.stat().st_ino
        first.close()
        source.unlink()
        second.bind(str(source))
        source.chmod(0o600)
        bridge.link_socket(source, target, owner)
        assert target.stat().st_ino == source.stat().st_ino != old_inode
        assert stat.S_ISSOCK(target.stat().st_mode)
    finally:
        first.close()
        second.close()
