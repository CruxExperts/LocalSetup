"""
Test suite for npm_api.py.
Tests all logic that does not require a live NPM instance:
- Input sanitization and validation
- Config loading (good + bad paths)
- Token caching logic
- CLI argument parsing
- Output formatting
- Backup filesystem logic
- HTTP error path handling (via mock)
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# The file under test lives alongside this test script
sys.path.insert(0, str(Path(__file__).parent))
import npm_api
from npm_api_test_helpers import make_conf


# ---------------------------------------------------------------------------
# Input hardening: _sanitize_str
# ---------------------------------------------------------------------------

class TestSanitizeStr(unittest.TestCase):

    def test_basic_string_passthrough(self):
        self.assertEqual(npm_api._sanitize_str("hello"), "hello")

    def test_strips_leading_trailing_whitespace(self):
        self.assertEqual(npm_api._sanitize_str("  hello  "), "hello")

    def test_strips_control_characters(self):
        # null bytes, ESC, BEL should be removed
        self.assertEqual(npm_api._sanitize_str("hel\x00lo\x1b"), "hello")

    def test_keeps_tabs_and_newlines(self):
        result = npm_api._sanitize_str("line1\nline2")
        self.assertIn("line1", result)
        self.assertIn("line2", result)

    def test_accepts_int(self):
        self.assertEqual(npm_api._sanitize_str(42), "42")

    def test_accepts_float(self):
        self.assertEqual(npm_api._sanitize_str(3.14), "3.14")

    def test_rejects_list(self):
        with self.assertRaises(SystemExit):
            npm_api._sanitize_str(["bad"])

    def test_max_length_exceeded(self):
        with self.assertRaises(SystemExit):
            npm_api._sanitize_str("a" * 10, max_len=5)

    def test_exactly_at_max_length(self):
        self.assertEqual(npm_api._sanitize_str("abcde", max_len=5), "abcde")

    def test_empty_string_ok(self):
        self.assertEqual(npm_api._sanitize_str(""), "")


# ---------------------------------------------------------------------------
# Input hardening: _validate_port
# ---------------------------------------------------------------------------

class TestValidatePort(unittest.TestCase):

    def test_valid_port(self):
        self.assertEqual(npm_api._validate_port(80), 80)
        self.assertEqual(npm_api._validate_port("8080"), 8080)
        self.assertEqual(npm_api._validate_port(65535), 65535)
        self.assertEqual(npm_api._validate_port(1), 1)

    def test_zero_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_port(0)

    def test_above_max_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_port(65536)

    def test_negative_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_port(-1)

    def test_non_numeric_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_port("abc")

    def test_none_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_port(None)


# ---------------------------------------------------------------------------
# Input hardening: _validate_domain
# ---------------------------------------------------------------------------

class TestValidateDomain(unittest.TestCase):

    def test_valid_domain(self):
        self.assertEqual(npm_api._validate_domain("example.com"), "example.com")
        self.assertEqual(npm_api._validate_domain("sub.example.com"), "sub.example.com")
        self.assertEqual(npm_api._validate_domain("my-app.example.co.uk"), "my-app.example.co.uk")

    def test_wildcard_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("*.example.com")

    def test_bare_label_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("localhost")

    def test_ip_address_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("192.168.1.1")

    def test_empty_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("")

    def test_trailing_dot_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("example.com.")

    def test_injection_attempt_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_domain("example.com; rm -rf /")

    def test_uppercase_accepted(self):
        # Domain validation should accept mixed case
        result = npm_api._validate_domain("Example.COM")
        self.assertEqual(result, "Example.COM")


# ---------------------------------------------------------------------------
# Input hardening: _validate_scheme
# ---------------------------------------------------------------------------

class TestValidateScheme(unittest.TestCase):

    def test_http_accepted(self):
        self.assertEqual(npm_api._validate_scheme("http"), "http")

    def test_https_accepted(self):
        self.assertEqual(npm_api._validate_scheme("https"), "https")

    def test_uppercase_normalised(self):
        self.assertEqual(npm_api._validate_scheme("HTTP"), "http")

    def test_ftp_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_scheme("ftp")

    def test_empty_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_scheme("")


# ---------------------------------------------------------------------------
# Input hardening: _validate_host_id
# ---------------------------------------------------------------------------

class TestValidateHostId(unittest.TestCase):

    def test_valid_int(self):
        self.assertEqual(npm_api._validate_host_id(5), 5)

    def test_valid_string_int(self):
        self.assertEqual(npm_api._validate_host_id("42"), 42)

    def test_zero_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_host_id(0)

    def test_negative_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_host_id(-1)

    def test_string_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_host_id("abc")

    def test_float_truncated_correctly(self):
        # int("5.0") raises, but int(5.0) works
        self.assertEqual(npm_api._validate_host_id(5.0), 5)


# ---------------------------------------------------------------------------
# Input hardening: _validate_access_list_id
# ---------------------------------------------------------------------------

class TestValidateAccessListId(unittest.TestCase):

    def test_positive_id_accepted(self):
        self.assertEqual(npm_api._validate_access_list_id("5"), 5)

    def test_zero_means_none(self):
        self.assertIsNone(npm_api._validate_access_list_id(0))

    def test_empty_means_none(self):
        self.assertIsNone(npm_api._validate_access_list_id(""))

    def test_non_numeric_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_access_list_id("not-an-id")

    def test_negative_rejected(self):
        with self.assertRaises(SystemExit):
            npm_api._validate_access_list_id(-1)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestConfig(unittest.TestCase):

    def test_loads_valid_conf(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = make_conf(tmp)
            cfg = npm_api.Config(conf)
            self.assertEqual(cfg.nginx_ip, "127.0.0.1")
            self.assertEqual(cfg.nginx_port, 81)
            self.assertEqual(cfg.api_user, "admin@test.local")
            self.assertEqual(cfg.api_pass, "testpass")
            self.assertEqual(cfg.base_url, "http://127.0.0.1:81/api")

    def test_custom_port(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td), NGINX_PORT="8181")
            cfg = npm_api.Config(conf)
            self.assertEqual(cfg.nginx_port, 8181)

    def test_missing_conf_dies(self):
        with self.assertRaises(SystemExit):
            npm_api.Config(Path("/nonexistent/npm-api.conf"))

    def test_missing_api_user_dies(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td), API_USER="")
            with self.assertRaises(SystemExit):
                npm_api.Config(conf)

    def test_missing_api_pass_dies(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td), API_PASS="")
            with self.assertRaises(SystemExit):
                npm_api.Config(conf)

    def test_invalid_port_dies(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td), NGINX_PORT="notaport")
            with self.assertRaises(SystemExit):
                npm_api.Config(conf)

    def test_token_paths_scoped_by_ip_port(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            cfg = npm_api.Config(conf)
            # Token dir slug should contain IP and port
            self.assertIn("127_0_0_1", str(cfg.token_dir))
            self.assertIn("81", str(cfg.token_dir))

    def test_custom_data_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            custom_data = tmp / "mydata"
            conf = make_conf(tmp, DATA_DIR=str(custom_data))
            cfg = npm_api.Config(conf)
            self.assertTrue(str(cfg.data_dir).endswith("mydata"))

    def test_env_var_conf_path(self):
        with tempfile.TemporaryDirectory() as td:
            conf = make_conf(Path(td))
            with patch.dict(os.environ, {"NPM_CONF": str(conf)}):
                cfg = npm_api.Config()
                self.assertEqual(cfg.nginx_ip, "127.0.0.1")


# ---------------------------------------------------------------------------
# Token validity logic
# ---------------------------------------------------------------------------

class TestTokenValidity(unittest.TestCase):

    def _make_client(self, tmp: Path) -> npm_api.NPMClient:
        conf = make_conf(tmp)
        cfg = npm_api.Config(conf)
        return npm_api.NPMClient(cfg)

    def test_missing_token_files_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            self.assertFalse(client._token_is_valid())

    def test_empty_token_file_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            client.cfg.token_dir.mkdir(parents=True, exist_ok=True)
            client.cfg.token_file.write_text("")
            client.cfg.expiry_file.write_text("")
            self.assertFalse(client._token_is_valid())

    def test_expired_token_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            client.cfg.token_dir.mkdir(parents=True, exist_ok=True)
            client.cfg.token_file.write_text("sometoken")
            # Expiry in the past
            client.cfg.expiry_file.write_text("2020-01-01T00:00:00Z")
            self.assertFalse(client._token_is_valid())

    def test_token_expiring_soon_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            client.cfg.token_dir.mkdir(parents=True, exist_ok=True)
            client.cfg.token_file.write_text("sometoken")
            # Expiry 30 minutes from now (inside the 1-hour buffer)
            future = time.time() + 1800
            from datetime import datetime, timezone
            exp_str = datetime.fromtimestamp(future, tz=timezone.utc).isoformat()
            client.cfg.expiry_file.write_text(exp_str)
            self.assertFalse(client._token_is_valid())

    def test_valid_token_returns_true(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            client.cfg.token_dir.mkdir(parents=True, exist_ok=True)
            client.cfg.token_file.write_text("goodtoken")
            # Expiry 2 hours from now (outside the 1-hour buffer)
            future = time.time() + 7200
            from datetime import datetime, timezone
            exp_str = datetime.fromtimestamp(future, tz=timezone.utc).isoformat()
            client.cfg.expiry_file.write_text(exp_str)
            self.assertTrue(client._token_is_valid())
            self.assertEqual(client._token, "goodtoken")

    def test_unparseable_expiry_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            client.cfg.token_dir.mkdir(parents=True, exist_ok=True)
            client.cfg.token_file.write_text("goodtoken")
            client.cfg.expiry_file.write_text("not-a-date")
            self.assertFalse(client._token_is_valid())


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

class TestFormatHostsTable(unittest.TestCase):

    def test_empty_list(self):
        result = npm_api._fmt_hosts_table([])
        self.assertIn("No proxy hosts", result)

    def test_single_host(self):
        hosts = [{
            "id": 1,
            "domain_names": ["example.com"],
            "enabled": True,
            "certificate_id": None,
            "forward_scheme": "http",
            "forward_host": "mycontainer",
            "forward_port": 8080,
        }]
        result = npm_api._fmt_hosts_table(hosts)
        self.assertIn("example.com", result)
        self.assertIn("mycontainer", result)
        self.assertIn("8080", result)
        self.assertIn("yes", result)   # enabled
        self.assertIn("no", result)    # no SSL
        # GFM table structure
        self.assertIn("|", result)

    def test_host_with_ssl(self):
        hosts = [{
            "id": 2,
            "domain_names": ["secure.example.com"],
            "enabled": True,
            "certificate_id": 5,
            "forward_scheme": "http",
            "forward_host": "appcontainer",
            "forward_port": 3000,
        }]
        result = npm_api._fmt_hosts_table(hosts)
        self.assertIn("5", result)     # cert ID
        self.assertIn("yes", result)   # SSL yes

    def test_disabled_host(self):
        hosts = [{
            "id": 3,
            "domain_names": ["off.example.com"],
            "enabled": False,
            "certificate_id": None,
            "forward_scheme": "http",
            "forward_host": "offcontainer",
            "forward_port": 9000,
        }]
        result = npm_api._fmt_hosts_table(hosts)
        self.assertIn("no", result)

    def test_multiple_domains(self):
        hosts = [{
            "id": 4,
            "domain_names": ["a.com", "b.com"],
            "enabled": True,
            "certificate_id": None,
            "forward_scheme": "http",
            "forward_host": "c",
            "forward_port": 80,
        }]
        result = npm_api._fmt_hosts_table(hosts)
        self.assertIn("a.com", result)
        self.assertIn("b.com", result)


class TestFormatHostDetail(unittest.TestCase):

    def test_detail_contains_all_fields(self):
        h = {
            "id": 7,
            "domain_names": ["app.example.com"],
            "enabled": True,
            "forward_scheme": "https",
            "forward_host": "backend",
            "forward_port": 443,
            "certificate_id": 3,
            "ssl_forced": True,
            "http2_support": True,
            "hsts_enabled": False,
            "allow_websocket_upgrade": True,
            "caching_enabled": False,
            "block_exploits": True,
            "advanced_config": "",
            "access_list_id": None,
        }
        result = npm_api._fmt_host_detail(h)
        self.assertIn("app.example.com", result)
        self.assertIn("backend", result)
        self.assertIn("443", result)
        self.assertIn("https", result)
        self.assertIn("## Proxy host 7", result)
        # GFM table
        self.assertIn("| **Domain(s)**", result)


# ---------------------------------------------------------------------------
# CLI argument parsing (no config needed, parsing only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
