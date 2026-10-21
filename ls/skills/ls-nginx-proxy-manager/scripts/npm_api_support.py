"""Validation, config, formatting, and backup helpers for npm_api.py."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from configparser import ConfigParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONF = SCRIPT_DIR / "npm-api.conf"
TOKEN_EXPIRY_HOURS = 24
TOKEN_REFRESH_BUFFER_SECONDS = 3600
MAX_FIELD_LENGTH = 4096
MAX_DOMAIN_LENGTH = 253
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
DEBUG = os.environ.get("LOCALSETUP_DEBUG", "0") == "1"


def die(msg: str, exc: BaseException | None = None) -> None:
    """Emit actionable error to stderr and exit non-zero."""
    print(f"[npm_api ERROR] {msg}", file=sys.stderr)
    if exc is not None and DEBUG:
        import traceback

        traceback.print_exc(file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"[npm_api WARN] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[npm_api DEBUG] {msg}", file=sys.stderr)


def sanitize_str(value: Any, max_len: int = MAX_FIELD_LENGTH, field: str = "field") -> str:
    """Normalize and validate a string from external input."""
    if not isinstance(value, (str, int, float)):
        die(f"Expected string for {field}, got {type(value).__name__}")
    raw = str(value)
    cleaned = "".join(
        ch
        for ch in raw
        if unicodedata.category(ch)[0] != "C" or ch in ("\t", "\n", "\r")
    )
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        die(f"{field} exceeds maximum length {max_len}: got {len(cleaned)} chars")
    return cleaned


def validate_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        die(f"Invalid port value: {value!r} (must be an integer)")
    if not 1 <= port <= 65535:
        die(f"Port {port} is out of valid range 1-65535")
    return port


def validate_domain(domain: str) -> str:
    domain = sanitize_str(domain, MAX_DOMAIN_LENGTH, "domain")
    pattern = re.compile(
        r"^(\*\.)?([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
    )
    if not pattern.match(domain):
        die(f"Invalid domain name: {domain!r}")
    if domain.startswith("*."):
        die(f"Wildcard domains are not allowed for proxy host creation: {domain!r}")
    return domain


def validate_scheme(scheme: str) -> str:
    scheme = sanitize_str(scheme, 8, "scheme").lower()
    if scheme not in ("http", "https"):
        die(f"Invalid forward_scheme {scheme!r}; must be 'http' or 'https'")
    return scheme


def validate_host_id(value: Any) -> int:
    try:
        host_id = int(value)
    except (TypeError, ValueError):
        die(f"Invalid host ID: {value!r} (must be an integer)")
    if host_id < 1:
        die(f"Host ID must be a positive integer, got {host_id}")
    return host_id


def validate_access_list_id(value: Any) -> int | None:
    """Validate an NPM access list ID; 0/empty clears the access list."""
    if value is None or value == "":
        return None
    try:
        acl_id = int(value)
    except (TypeError, ValueError):
        die(f"Invalid access_list_id: {value!r} (must be an integer)")
    if acl_id < 0:
        die(f"Access list ID must be 0 or a positive integer, got {acl_id}")
    return acl_id or None


class Config:
    """Load and validate npm-api.conf."""

    def __init__(self, conf_path: Path | None = None) -> None:
        path = conf_path or Path(os.environ.get("NPM_CONF", str(DEFAULT_CONF)))
        if not path.exists():
            die(
                f"Config file not found: {path}\n"
                "  Create it with: NGINX_IP, NGINX_PORT, API_USER, API_PASS\n"
                "  See references/npm-api-conf-example.md for the current template."
            )
        if oct(path.stat().st_mode)[-3:] not in ("600", "400"):
            warn(f"Config file {path} is world-readable; run: chmod 600 {path}")

        raw = path.read_text(encoding="utf-8", errors="replace")
        cp = ConfigParser(interpolation=None)
        cp.read_string("[conf]\n" + raw)
        section = cp["conf"]

        self.nginx_ip = sanitize_str(section.get("NGINX_IP", "127.0.0.1"), 64, "NGINX_IP")
        self.nginx_port = validate_port(section.get("NGINX_PORT", "81"))
        self.api_user = sanitize_str(section.get("API_USER", ""), 256, "API_USER")
        self.api_pass = sanitize_str(section.get("API_PASS", ""), 256, "API_PASS")

        if not self.api_user or not self.api_pass:
            die("API_USER and API_PASS must be set in the config file")

        raw_data_dir = section.get("DATA_DIR", str(SCRIPT_DIR / "data"))
        self.data_dir = Path(sanitize_str(raw_data_dir, 512, "DATA_DIR")).expanduser().resolve()
        self.base_url = f"http://{self.nginx_ip}:{self.nginx_port}/api"

        slug = f"{self.nginx_ip.replace('.', '_')}_{self.nginx_port}"
        self.token_dir = self.data_dir / slug / "token"
        self.token_file = self.token_dir / "token.txt"
        self.expiry_file = self.token_dir / "expiry.txt"
        self.backup_dir = self.data_dir / slug / "backups"


def backup_client(client: Any, backup_dir: Path | None = None) -> Path:
    """Snapshot NPM API resources to JSON files and return the created directory."""
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y_%m_%d__%H_%M_%S")
    out = (backup_dir or client.cfg.backup_dir) / timestamp
    out.mkdir(parents=True, mode=0o700, exist_ok=True)

    endpoints: list[tuple[str, str]] = [
        ("/nginx/proxy-hosts", "proxy_hosts"),
        ("/users", "users"),
        ("/settings", "settings"),
        ("/nginx/access-lists", "access_lists"),
        ("/nginx/certificates", "certificates"),
    ]
    summary: dict[str, int] = {}
    errors: list[str] = []

    for api_path, name in endpoints:
        try:
            data = client.request("GET", api_path)
            dest = out / f"{name}.json"
            dest.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.chmod(dest, 0o600)
            count = len(data) if isinstance(data, list) else 1
            summary[name] = count
            debug(f"Backed up {name}: {count} item(s)")
        except SystemExit:
            errors.append(name)

    manifest = out / "manifest.json"
    manifest.write_text(
        json.dumps({"backup_timestamp": timestamp, "items": summary}, indent=2),
        encoding="utf-8",
    )
    os.chmod(manifest, 0o600)

    if errors:
        warn(f"Backup completed with errors on: {', '.join(errors)}")

    return out


def fmt_hosts_table(hosts: list[dict]) -> str:
    if not hosts:
        return "*No proxy hosts found.*"
    lines = [
        "| ID | Domain | Enabled | SSL | Target | Cert ID |",
        "|----|--------|---------|-----|--------|---------|",
    ]
    for host in hosts:
        host_id = host.get("id", "?")
        domains = ", ".join(host.get("domain_names", []))
        enabled = "yes" if host.get("enabled") else "no"
        cert_id = host.get("certificate_id") or "-"
        ssl = "yes" if cert_id != "-" else "no"
        scheme = host.get("forward_scheme", "http")
        forward_host = host.get("forward_host", "?")
        forward_port = host.get("forward_port", "?")
        target = f"`{scheme}://{forward_host}:{forward_port}`"
        lines.append(f"| {host_id} | {domains} | {enabled} | {ssl} | {target} | {cert_id} |")
    return "\n".join(lines)


def fmt_host_detail(host: dict) -> str:
    host_id = host.get("id", "?")
    domains = ", ".join(host.get("domain_names", []))
    enabled = "yes" if host.get("enabled") else "no"
    scheme = host.get("forward_scheme", "http")
    forward_host = host.get("forward_host", "?")
    forward_port = host.get("forward_port", "?")
    cert_id = host.get("certificate_id") or "-"
    ssl_forced = "yes" if host.get("ssl_forced") else "no"
    http2 = "yes" if host.get("http2_support") else "no"
    hsts = "yes" if host.get("hsts_enabled") else "no"
    websocket = "yes" if host.get("allow_websocket_upgrade") else "no"
    caching = "yes" if host.get("caching_enabled") else "no"
    exploits = "yes" if host.get("block_exploits") else "no"
    advanced = host.get("advanced_config") or "-"
    access_list_id = host.get("access_list_id") or "-"

    return (
        f"## Proxy host {host_id}\n\n"
        f"| Field | Value |\n"
        f"|-------|-------|\n"
        f"| **Domain(s)** | {domains} |\n"
        f"| **Enabled** | {enabled} |\n"
        f"| **Target** | `{scheme}://{forward_host}:{forward_port}` |\n"
        f"| **Certificate ID** | {cert_id} |\n"
        f"| **SSL forced** | {ssl_forced} |\n"
        f"| **HTTP/2** | {http2} |\n"
        f"| **HSTS** | {hsts} |\n"
        f"| **WebSocket upgrade** | {websocket} |\n"
        f"| **Caching** | {caching} |\n"
        f"| **Block exploits** | {exploits} |\n"
        f"| **Access list ID** | {access_list_id} |\n"
        f"| **Advanced config** | {advanced} |\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="npm_api.py",
        description="Nginx Proxy Manager API client (Python, no shell dependencies)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 npm_api.py --info\n"
            "  python3 npm_api.py --host-list\n"
            "  python3 npm_api.py --host-create example.com -i mycontainer -p 8080\n"
            "  python3 npm_api.py --host-create app.example.com -i appcontainer -p 3000 --websocket\n"
            "  python3 npm_api.py --host-search example.com\n"
            "  python3 npm_api.py --host-show 5\n"
            "  python3 npm_api.py --host-update 5 forward_port=9000\n"
            "  python3 npm_api.py --host-enable 5\n"
            "  python3 npm_api.py --host-disable 5\n"
            "  python3 npm_api.py --host-delete 5\n"
            "  python3 npm_api.py --backup\n"
        ),
    )

    ops = parser.add_mutually_exclusive_group(required=True)
    ops.add_argument("--info", action="store_true", help="Check API connectivity and token")
    ops.add_argument("--host-list", action="store_true", help="List all proxy hosts")
    ops.add_argument("--host-search", metavar="DOMAIN", help="Search proxy hosts by domain")
    ops.add_argument("--host-show", metavar="ID", help="Show details for a proxy host ID")
    ops.add_argument("--host-create", metavar="DOMAIN", help="Create a proxy host for DOMAIN")
    ops.add_argument("--host-update", metavar="ID", help="Update fields on a proxy host ID")
    ops.add_argument("--host-enable", metavar="ID", help="Enable a proxy host by ID")
    ops.add_argument("--host-disable", metavar="ID", help="Disable a proxy host by ID")
    ops.add_argument("--host-delete", metavar="ID", help="Delete a proxy host by ID")
    ops.add_argument("--backup", action="store_true", help="Backup NPM configuration to DATA_DIR")

    parser.add_argument("-i", "--forward-host", metavar="HOST", help="Backend container name or hostname")
    parser.add_argument("-p", "--forward-port", metavar="PORT", type=int, help="Backend port")
    parser.add_argument(
        "--scheme",
        metavar="SCHEME",
        default="http",
        help="forward_scheme: http or https (default: http)",
    )
    parser.add_argument("--websocket", action="store_true", help="Enable WebSocket upgrade")
    parser.add_argument(
        "--no-ssl-force",
        action="store_true",
        help="Do not force SSL redirect (default: ssl_forced=true)",
    )
    parser.add_argument("--no-http2", action="store_true", help="Disable HTTP/2 (default: enabled)")
    parser.add_argument("--hsts", action="store_true", help="Enable HSTS header")
    parser.add_argument("--caching", action="store_true", help="Enable NPM caching")
    parser.add_argument(
        "--no-block-exploits",
        action="store_true",
        help="Disable exploit blocking (default: enabled)",
    )
    parser.add_argument(
        "--access-list-id",
        metavar="ACL_ID",
        type=int,
        default=0,
        help="NPM access list ID (default: 0 = none)",
    )
    parser.add_argument("--advanced-config", metavar="CONFIG", default="", help="Raw nginx config block")
    parser.add_argument("fields", nargs="*", help="KEY=VALUE pairs for --host-update")
    parser.add_argument("--conf", metavar="PATH", help="Path to npm-api.conf (overrides NPM_CONF env)")
    parser.add_argument("--backup-dir", metavar="PATH", help="Override backup output directory")

    return parser
