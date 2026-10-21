"""Shared fixtures for npm_api tests."""

from __future__ import annotations

from pathlib import Path


def make_conf(tmp: Path, **overrides) -> Path:
    """Write a minimal valid npm-api.conf and return its path."""
    fields = {
        "NGINX_IP": "127.0.0.1",
        "NGINX_PORT": "81",
        "API_USER": "admin@test.local",
        "API_PASS": "testpass",
        "DATA_DIR": str(tmp / "data"),
    }
    fields.update(overrides)
    conf = tmp / "npm-api.conf"
    conf.write_text("\n".join(f"{k}={v}" for k, v in fields.items()))
    conf.chmod(0o600)
    return conf
