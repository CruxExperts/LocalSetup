#!/usr/bin/env python3
"""Validate public HTTPS endpoint syntax without printing private URL data."""
from __future__ import annotations

import argparse
import ipaddress
import re
from urllib.parse import urlsplit


def hostname(value: str) -> str:
    host = value.rstrip('.').lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('IP address endpoints are forbidden')
    if ('.' not in host or len(host) > 253
            or host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test'))
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in host.split('.'))):
        raise ValueError('public DNS hostname required')
    return host


def endpoint(value: str, *, allow_path: bool = False, allow_query: bool = False) -> str:
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('whitespace and control characters are forbidden')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('invalid URL') from None
    if (parts.scheme != 'https' or parts.username is not None
            or parts.password is not None or parts.fragment or port not in (None, 443)
            or (parts.query and not allow_query)
            or (parts.path not in ('', '/') and not allow_path)):
        raise ValueError('verified public HTTPS on port 443 required')
    return hostname(parts.hostname or '')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='Nonsecret endpoint; never pass signed URLs on CLI')
    parser.add_argument('--allow-path', action='store_true')
    args = parser.parse_args()
    try:
        endpoint(args.url, allow_path=args.allow_path)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    print('Endpoint form accepted; verify DNS and TLS with the actual client')


if __name__ == '__main__':
    main()
