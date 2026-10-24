"""Blind framing and owned I/O limits; cryptographic endpoint proof is separate."""
import os
import socket
import threading
import time

import pytest

from ls.core.agent.control_transport import (
    HEADER, MAX_CIPHERTEXT, Endpoint, Stream, TransportError, header, pack, unpack,
)


def pair(seconds=2, cancelled=None):
    left, right = socket.socketpair()
    fd = left.detach()
    return Stream(fd, fd, expires=time.monotonic()+seconds, cancelled=cancelled), right


def receive(sock):
    def exact(size):
        raw = b''
        while len(raw) < size:
            part = sock.recv(size-len(raw))
            if not part:
                raise EOFError
            raw += part
        return raw
    raw = exact(HEADER.size)
    return raw+exact(header(raw)[3])


@pytest.mark.parametrize('damage', ['short', 'trailing', 'version', 'oversize'])
def test_framing_rejects_invalid_bytes_before_payload_acceptance(damage):
    raw = pack('execute', 'a'*32, b'opaque-ciphertext')
    assert unpack(raw) == ('execute', 'a'*32, False, b'opaque-ciphertext')
    if damage == 'short':
        raw = raw[:-1]
    elif damage == 'trailing':
        raw += b'extra'
    elif damage == 'version':
        raw = b'LSC2'+raw[4:]
    else:
        raw = HEADER.pack(b'LSC1', 1, 0, bytes.fromhex('a'*32), MAX_CIPHERTEXT+1)
    with pytest.raises(ValueError):
        unpack(raw)


@pytest.mark.parametrize('failure', ['disconnect', 'wrong_id', 'oversize'])
def test_lost_or_invalid_reply_closes_stream_without_replay(failure):
    client, peer = pair()
    seen = []
    def server():
        with peer:
            seen.append(unpack(receive(peer)))
            if failure == 'wrong_id':
                peer.sendall(pack('execute', 'b'*32, b'reply', reply=True))
            elif failure == 'oversize':
                peer.sendall(HEADER.pack(b'LSC1', 1, 1, bytes.fromhex('a'*32), MAX_CIPHERTEXT+1))
    worker = threading.Thread(target=server); worker.start()
    try:
        with pytest.raises(TransportError) as caught:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert caught.value.outcome == 'uncertain' and client.closed
        with pytest.raises(TransportError) as repeated:
            client.exchange('execute', 'a'*32, b'ciphertext')
        assert repeated.value.outcome == 'not_dispatched'
    finally:
        client.close(); worker.join(2)
    assert not worker.is_alive() and len(seen) == 1


@pytest.mark.parametrize('cancel', [False, True])
@pytest.mark.parametrize('stall', ['read', 'write'])
def test_backpressure_obeys_deadline_and_cancellation(cancel, stall):
    cancelled = threading.Event()
    client, peer = pair(seconds=2 if cancel else .1, cancelled=cancelled)
    timer = threading.Timer(.1, cancelled.set) if cancel else None
    if timer:
        timer.start()
    start = time.monotonic()
    try:
        with pytest.raises(TransportError) as caught:
            client.exchange('execute', 'a'*32, b'x'*(MAX_CIPHERTEXT if stall == 'write' else 16))
        assert caught.value.outcome == 'uncertain' and time.monotonic()-start < 1
    finally:
        client.close(); peer.close()
        if timer:
            timer.join(1)


@pytest.mark.parametrize('transport', ['socket', 'pipes'])
def test_both_stream_directions_preserve_exact_opaque_bytes(transport):
    class Echo(Endpoint):
        def __init__(self):
            pass

        def respond(self, raw):
            kind, identifier, reply, ciphertext = unpack(raw)
            assert not reply
            return pack(kind, identifier, ciphertext, reply=True)

    if transport == 'socket':
        client, peer = pair()
        fd = peer.detach()
        server = Stream(fd, fd, expires=time.monotonic()+2)
    else:
        server_read, client_write = os.pipe()
        client_read, server_write = os.pipe()
        client = Stream(client_read, client_write, expires=time.monotonic()+2)
        server = Stream(server_read, server_write, expires=time.monotonic()+2)
    errors = []
    def serve():
        try:
            server.serve_once(Echo())
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=serve); worker.start()
    try:
        assert client.exchange('reconcile', 'c'*32, b'\x00opaque\xff') == b'\x00opaque\xff'
    finally:
        worker.join(2); client.close(); server.close()
    assert not worker.is_alive() and errors == []
