#!/usr/bin/env python3
"""Relay allowlisted TLS ClientHello names without terminating their TLS."""

from __future__ import annotations

import asyncio
import ipaddress
import argparse
import socket


def client_hello_name(handshake: bytes) -> str:
    if len(handshake) < 4 or handshake[0] != 1:
        raise ValueError("TLS ClientHello required")
    size = int.from_bytes(handshake[1:4], "big")
    body = handshake[4:4 + size]
    if len(body) != size:
        raise ValueError("truncated handshake")
    offset = 0

    def take(length: int) -> bytes:
        nonlocal offset
        value = body[offset:offset + length]
        if len(value) != length:
            raise ValueError("truncated ClientHello field")
        offset += length
        return value

    def number(length: int) -> int:
        return int.from_bytes(take(length), "big")

    take(34)  # legacy version and random
    take(number(1))  # session identifier
    ciphers = number(2)
    if not ciphers or ciphers % 2:
        raise ValueError("invalid cipher list")
    take(ciphers)
    compression = number(1)
    if not compression:
        raise ValueError("missing compression methods")
    take(compression)
    extensions = take(number(2))
    if offset != len(body):
        raise ValueError("invalid extension length")
    names = []
    position = 0
    while position < len(extensions):
        if len(extensions) - position < 4:
            raise ValueError("truncated extension header")
        kind = int.from_bytes(extensions[position:position + 2], "big")
        length = int.from_bytes(extensions[position + 2:position + 4], "big")
        position += 4
        data = extensions[position:position + length]
        if len(data) != length:
            raise ValueError("truncated extension")
        position += length
        if kind != 0:
            continue
        if len(data) < 5 or int.from_bytes(data[:2], "big") != len(data) - 2:
            raise ValueError("invalid SNI extension")
        if data[2] != 0 or int.from_bytes(data[3:5], "big") != len(data) - 5:
            raise ValueError("one DNS server name is required")
        names.append(data[5:].decode("ascii").lower())
    if len(names) != 1:
        raise ValueError("exactly one SNI extension is required")
    return names[0]


async def read_client_hello(reader: asyncio.StreamReader) -> tuple[str, bytes]:
    wire = bytearray()
    handshake = bytearray()
    for _ in range(8):
        header = await reader.readexactly(5)
        length = int.from_bytes(header[3:], "big")
        if header[0] != 22 or header[1] != 3 or not 0 < length <= 18432:
            raise ValueError("TLS handshake records required")
        payload = await reader.readexactly(length)
        wire.extend(header + payload)
        handshake.extend(payload)
        if len(handshake) > 65536:
            raise ValueError("ClientHello exceeds limit")
        if len(handshake) >= 4 and len(handshake) >= 4 + int.from_bytes(handshake[1:4], "big"):
            return client_hello_name(bytes(handshake)), bytes(wire)
    raise ValueError("too many handshake fragments")


async def public_address(hostname: str) -> str:
    entries = await asyncio.get_running_loop().getaddrinfo(
        hostname, 443, family=socket.AF_INET, type=socket.SOCK_STREAM,
    )
    addresses = [item[4][0] for item in entries]
    if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
        raise ValueError("upstream DNS must resolve to public addresses")
    return addresses[0]


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await asyncio.wait_for(reader.read(65536), timeout=120):
        writer.write(data)
        await writer.drain()


async def serve(allowed: set[str], listen: str, source_cidr: str, port: int) -> None:
    capacity = asyncio.Semaphore(64)
    private = ipaddress.ip_network(source_cidr)

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        tasks = []
        try:
            peer = writer.get_extra_info("peername")
            if not peer or ipaddress.ip_address(peer[0]) not in private:
                return
            if capacity.locked():
                return
            async with asyncio.timeout(3600), capacity:
                name, initial = await asyncio.wait_for(read_client_hello(reader), 10)
                if name not in allowed:
                    return
                address = await asyncio.wait_for(public_address(name), 10)
                remote, upstream = await asyncio.wait_for(asyncio.open_connection(address, 443), 10)
                upstream.write(initial)
                await upstream.drain()
                tasks = [asyncio.create_task(pump(reader, upstream)),
                         asyncio.create_task(pump(remote, writer))]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ValueError, UnicodeError, OSError, TimeoutError, asyncio.IncompleteReadError):
            pass  # Fail closed; never log request bytes or establish a plaintext fallback.
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 2)
                    except (OSError, TimeoutError):
                        pass

    server = await asyncio.start_server(connection, listen, port, limit=65536)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-host", action="append", required=True)
    parser.add_argument("--listen", required=True, help="Private listening IP")
    parser.add_argument("--source-cidr", required=True, help="Allowed client subnet")
    parser.add_argument("--port", type=int, default=443)
    args = parser.parse_args()
    from public_https import hostname
    allowed = {hostname(value) for value in args.allow_host}
    address = ipaddress.ip_address(args.listen)
    network = ipaddress.ip_network(args.source_cidr)
    if (not address.is_private or address.is_loopback or address not in network
            or not network.is_private or network.is_loopback):
        parser.error("listener must be a private non-loopback address in source subnet")
    if not 1 <= args.port <= 65535:
        parser.error("invalid listening port")
    asyncio.run(serve(allowed, args.listen, args.source_cidr, args.port))


if __name__ == "__main__":
    main()
