"""
Purpose: Native Python client for Nginx Proxy Manager REST API.
         Replaces the upstream npm-api.sh Bash script entirely.
         No shell, curl, or jq dependencies required.
Created: 2026-02-26
Last Updated: 2026-02-27
Requires: requests (see pyproject.toml and uv.lock)

Usage:
    python3 npm_api.py --info
    python3 npm_api.py --host-list
    python3 npm_api.py --host-create example.com -i mycontainer -p 8080
    python3 npm_api.py --host-delete 42
    python3 npm_api.py --backup

All operations require npm-api.conf in the same directory as this script,
or the path set via NPM_CONF environment variable.

Environment variables:
    NPM_CONF          Path to config file (default: <script_dir>/npm-api.conf)
    LOCALSETUP_DEBUG  Set to 1 before startup for redacted diagnostic events
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Resolve ls/lib/ from skills/ls-nginx-proxy-manager/scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from deps import require_deps  # noqa: E402

require_deps(["requests"])

import requests  # noqa: E402

from npm_api_support import CONNECT_TIMEOUT
from npm_api_support import MAX_DOMAIN_LENGTH
from npm_api_support import MAX_FIELD_LENGTH
from npm_api_support import READ_TIMEOUT
from npm_api_support import TOKEN_EXPIRY_HOURS
from npm_api_support import TOKEN_REFRESH_BUFFER_SECONDS
from npm_api_support import Config
from npm_api_support import backup_client as _backup_client
from npm_api_support import build_parser as _build_parser
from npm_api_support import debug as _debug
from npm_api_support import die as _die
from npm_api_support import fmt_host_detail as _fmt_host_detail
from npm_api_support import fmt_hosts_table as _fmt_hosts_table
from npm_api_support import sanitize_str as _sanitize_str
from npm_api_support import validate_access_list_id as _validate_access_list_id
from npm_api_support import validate_domain as _validate_domain
from npm_api_support import validate_host_id as _validate_host_id
from npm_api_support import validate_port as _validate_port
from npm_api_support import validate_scheme as _validate_scheme
from npm_api_support import warn as _warn


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class NPMClient:
    """Low-level HTTP client for the NPM REST API."""

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self._token: str | None = None
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # --- Token management ---------------------------------------------------

    def _ensure_token(self) -> str:
        """Return a valid bearer token, refreshing if needed."""
        if self._token_is_valid():
            assert self._token is not None
            return self._token
        self._refresh_token()
        assert self._token is not None
        return self._token

    def _token_is_valid(self) -> bool:
        tf, ef = self.cfg.token_file, self.cfg.expiry_file
        if not tf.exists() or not ef.exists():
            return False
        try:
            token   = tf.read_text(encoding="utf-8").strip()
            expiry  = ef.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        if not token or not expiry:
            return False
        try:
            # NPM returns ISO8601; parse it
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            _debug(f"Could not parse token expiry: {expiry!r}; will refresh")
            return False
        remaining = exp_dt.timestamp() - time.time()
        if remaining < TOKEN_REFRESH_BUFFER_SECONDS:
            _debug(f"Token expires in {remaining:.0f}s; refreshing")
            return False
        self._token = token
        return True

    def _refresh_token(self) -> None:
        """Obtain a new bearer token from NPM and cache it."""
        _debug("Requesting new API token")

        # Step 1: short-lived token (no auth header)
        resp = self._raw_request("POST", "/tokens", {
            "identity": self.cfg.api_user,
            "secret":   self.cfg.api_pass,
        }, auth=False)
        short_token = resp.get("token")
        if not short_token:
            _die(
                "Token endpoint returned no token field.\n"
                f"  Check NGINX_IP={self.cfg.nginx_ip}, NGINX_PORT={self.cfg.nginx_port},\n"
                "  API_USER and API_PASS in npm-api.conf."
            )

        # Step 2: long-lived token
        resp2 = self._raw_request(
            "GET",
            f"/tokens?expiry={TOKEN_EXPIRY_HOURS}h",
            auth_token=short_token,
        )
        token  = resp2.get("token")
        expiry = resp2.get("expires")
        if not token or not expiry:
            _die("Failed to obtain long-lived token from NPM")

        # Persist token files, then update session Authorization header
        self.cfg.token_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.cfg.token_file.write_text(token, encoding="utf-8")
        self.cfg.expiry_file.write_text(expiry, encoding="utf-8")
        os.chmod(self.cfg.token_file, 0o600)
        os.chmod(self.cfg.expiry_file, 0o600)
        self._token = token
        self._session.headers.update({"Authorization": f"Bearer {token}"})
        _debug(f"Token cached; expires {expiry}")

    # --- Core HTTP ----------------------------------------------------------

    def _raw_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        auth: bool = True,
        auth_token: str | None = None,
    ) -> Any:
        """
        Execute a single HTTP request using the shared session.
        Returns parsed JSON body on success. Raises SystemExit on errors.
        """
        url = self.cfg.base_url + path
        _debug(f"{method} {url}")

        # Override Authorization for this single call when auth_token is given
        # (used during token bootstrap before the session header is set).
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif not auth:
            # Temporarily strip Authorization for unauthenticated calls
            headers["Authorization"] = ""

        try:
            resp = self._session.request(
                method,
                url,
                json=body,
                headers={k: v for k, v in headers.items() if v} or None,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            # Extract NPM's structured error message (behavioral parity)
            msg = ""
            if exc.response is not None:
                try:
                    err_json = exc.response.json()
                    msg = (
                        err_json.get("error", {}).get("message")
                        or err_json.get("message")
                        or exc.response.text
                    )
                except Exception:
                    msg = exc.response.text
            _die(
                f"HTTP {status} from {method} {url}\n"
                f"  {msg}\n"
                "  Check NPM admin credentials and that the API is reachable."
            )
        except requests.ConnectionError as exc:
            _die(
                f"Network error reaching {url}: {exc}\n"
                f"  Verify NGINX_IP={self.cfg.nginx_ip} and NGINX_PORT={self.cfg.nginx_port}."
            )
        except requests.RequestException as exc:
            _die(f"Request failed for {method} {url}: {exc}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            _die(
                f"Invalid JSON response from {method} {url}: {exc}\n"
                f"  Raw (first 200 chars): {resp.text[:200]!r}"
            )

    def request(self, method: str, path: str, body: dict | None = None) -> Any:
        """Authenticated request with automatic token refresh."""
        token = self._ensure_token()
        return self._raw_request(method, path, body, auth_token=token)

    # --- Connectivity check --------------------------------------------------

    def info(self) -> dict:
        return self.request("GET", "/tokens")

    # --- Proxy host operations ----------------------------------------------

    def host_list(self) -> list[dict]:
        data = self.request("GET", "/nginx/proxy-hosts")
        if not isinstance(data, list):
            _die(f"Expected list from host-list, got {type(data).__name__}")
        return data

    def host_search(self, domain: str) -> list[dict]:
        domain = _sanitize_str(domain, MAX_DOMAIN_LENGTH, "domain")
        hosts = self.host_list()
        domain_lower = domain.lower()
        return [
            h for h in hosts
            if any(d.lower() == domain_lower for d in h.get("domain_names", []))
        ]

    def host_show(self, host_id: int) -> dict:
        host_id = _validate_host_id(host_id)
        return self.request("GET", f"/nginx/proxy-hosts/{host_id}")

    def host_create(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        forward_scheme: str = "http",
        ssl_forced: bool = True,
        http2_support: bool = True,
        hsts_enabled: bool = False,
        caching_enabled: bool = False,
        block_exploits: bool = True,
        allow_websocket_upgrade: bool = False,
        access_list_id: int = 0,
        advanced_config: str = "",
        locations: list | None = None,
    ) -> dict:
        domain        = _validate_domain(domain)
        forward_host  = _sanitize_str(forward_host, 253, "forward_host")
        forward_port  = _validate_port(forward_port)
        forward_scheme = _validate_scheme(forward_scheme)
        access_list_id = _validate_access_list_id(access_list_id)
        advanced_config = _sanitize_str(advanced_config, MAX_FIELD_LENGTH, "advanced_config")

        payload: dict[str, Any] = {
            "domain_names":           [domain],
            "forward_host":           forward_host,
            "forward_port":           forward_port,
            "forward_scheme":         forward_scheme,
            "ssl_forced":             ssl_forced,
            "http2_support":          http2_support,
            "hsts_enabled":           hsts_enabled,
            "caching_enabled":        caching_enabled,
            "block_exploits":         block_exploits,
            "allow_websocket_upgrade": allow_websocket_upgrade,
            "access_list_id":         access_list_id,
            "certificate_id":         None,
            "advanced_config":        advanced_config,
            "meta":                   {"dns_challenge": None},
            "locations":              locations or [],
            "enabled":                True,
        }
        return self.request("POST", "/nginx/proxy-hosts", payload)

    def host_update(self, host_id: int, fields: dict) -> dict:
        """Patch specific fields on an existing proxy host (PATCH via PUT)."""
        host_id = _validate_host_id(host_id)
        # Validate any field we recognise; pass others through for forward-compat
        sanitized: dict[str, Any] = {}
        for key, val in fields.items():
            key = _sanitize_str(key, 64, "field name")
            if key == "forward_host":
                sanitized[key] = _sanitize_str(val, 253, key)
            elif key == "forward_port":
                sanitized[key] = _validate_port(val)
            elif key == "forward_scheme":
                sanitized[key] = _validate_scheme(val)
            elif key in ("ssl_forced", "http2_support", "hsts_enabled",
                         "caching_enabled", "block_exploits", "allow_websocket_upgrade",
                         "enabled"):
                if not isinstance(val, bool):
                    _die(f"Field {key!r} must be a boolean, got {type(val).__name__}")
                sanitized[key] = val
            elif key == "advanced_config":
                sanitized[key] = _sanitize_str(val, MAX_FIELD_LENGTH, key)
            elif key == "access_list_id":
                sanitized[key] = _validate_access_list_id(val)
            else:
                # Unknown field: pass through with basic string sanitization
                sanitized[key] = _sanitize_str(str(val), MAX_FIELD_LENGTH, key)
        return self.request("PUT", f"/nginx/proxy-hosts/{host_id}", sanitized)

    def host_enable(self, host_id: int) -> dict:
        host_id = _validate_host_id(host_id)
        return self.request("PUT", f"/nginx/proxy-hosts/{host_id}", {"enabled": True})

    def host_disable(self, host_id: int) -> dict:
        host_id = _validate_host_id(host_id)
        return self.request("PUT", f"/nginx/proxy-hosts/{host_id}", {"enabled": False})

    def host_delete(self, host_id: int) -> bool:
        host_id = _validate_host_id(host_id)
        self.request("DELETE", f"/nginx/proxy-hosts/{host_id}")
        return True

    # --- Backup -------------------------------------------------------------

    def backup(self, backup_dir: Path | None = None) -> Path:
        return _backup_client(self, backup_dir)


def main() -> None:  # noqa: C901 (complexity acceptable for CLI dispatcher)
    parser = _build_parser()
    args = parser.parse_args()

    conf_path = Path(args.conf).expanduser().resolve() if args.conf else None
    cfg = Config(conf_path)
    client = NPMClient(cfg)

    # --info
    if args.info:
        result = client.info()
        print("## NPM API connectivity\n")
        print("**Status:** OK")
        print(f"**Endpoint:** `{cfg.base_url}`")
        expires = result.get("expires", "unknown")
        print(f"**Token expires:** {expires}")
        return

    # --host-list
    if args.host_list:
        hosts = client.host_list()
        print("## Proxy hosts\n")
        print(_fmt_hosts_table(hosts))
        return

    # --host-search
    if args.host_search:
        domain = _sanitize_str(args.host_search, MAX_DOMAIN_LENGTH, "domain")
        hosts = client.host_search(domain)
        print(f"## Search: `{domain}`\n")
        print(_fmt_hosts_table(hosts))
        return

    # --host-show
    if args.host_show:
        hid = _validate_host_id(args.host_show)
        host = client.host_show(hid)
        print(_fmt_host_detail(host))
        return

    # --host-create
    if args.host_create:
        domain = args.host_create
        if not args.forward_host:
            _die("--host-create requires -i/--forward-host (container name or hostname)")
        if not args.forward_port:
            _die("--host-create requires -p/--forward-port")
        host = client.host_create(
            domain              = domain,
            forward_host        = args.forward_host,
            forward_port        = args.forward_port,
            forward_scheme      = args.scheme,
            ssl_forced          = not args.no_ssl_force,
            http2_support       = not args.no_http2,
            hsts_enabled        = args.hsts,
            caching_enabled     = args.caching,
            block_exploits      = not args.no_block_exploits,
            allow_websocket_upgrade = args.websocket,
            access_list_id      = args.access_list_id,
            advanced_config     = args.advanced_config,
        )
        hid = host.get("id", "?")
        print(f"## Proxy host created\n")
        print(f"**Domain:** `{domain}`  ")
        print(f"**ID:** {hid}  ")
        print(f"**Target:** `{args.scheme}://{args.forward_host}:{args.forward_port}`  ")
        print("\n> Note: attach an SSL certificate in the NPM UI if HTTPS is required.")
        return

    # --host-update
    if args.host_update:
        hid = _validate_host_id(args.host_update)
        if not args.fields:
            _die("--host-update requires at least one KEY=VALUE field argument")
        fields: dict[str, Any] = {}
        for item in args.fields:
            if "=" not in item:
                _die(f"Invalid field format {item!r}; expected KEY=VALUE")
            k, _, v = item.partition("=")
            fields[k.strip()] = v.strip()
        result = client.host_update(hid, fields)
        print(f"## Host {hid} updated\n")
        print(_fmt_host_detail(result))
        return

    # --host-enable
    if args.host_enable:
        hid = _validate_host_id(args.host_enable)
        client.host_enable(hid)
        print(f"**Host {hid} enabled.**")
        return

    # --host-disable
    if args.host_disable:
        hid = _validate_host_id(args.host_disable)
        client.host_disable(hid)
        print(f"**Host {hid} disabled.**")
        return

    # --host-delete
    if args.host_delete:
        hid = _validate_host_id(args.host_delete)
        client.host_delete(hid)
        print(f"**Host {hid} deleted.**")
        return

    # --backup
    if args.backup:
        backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None
        out = client.backup(backup_dir)
        print(f"## Backup complete\n")
        print(f"**Location:** `{out}`")
        return


if __name__ == "__main__":
    main()
