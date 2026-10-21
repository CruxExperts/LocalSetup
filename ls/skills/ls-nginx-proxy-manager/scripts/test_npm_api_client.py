"""CLI, backup, and HTTP behavior tests for npm_api.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import npm_api
import npm_api_support
from npm_api_test_helpers import make_conf


class TestCLIParsing(unittest.TestCase):

    def _parse(self, args):
        parser = npm_api._build_parser()
        return parser.parse_args(args)

    def test_info_flag(self):
        args = self._parse(["--info"])
        self.assertTrue(args.info)

    def test_host_list_flag(self):
        args = self._parse(["--host-list"])
        self.assertTrue(args.host_list)

    def test_host_create_with_required_args(self):
        args = self._parse(["--host-create", "example.com", "-i", "mycontainer", "-p", "8080"])
        self.assertEqual(args.host_create, "example.com")
        self.assertEqual(args.forward_host, "mycontainer")
        self.assertEqual(args.forward_port, 8080)

    def test_host_create_defaults(self):
        args = self._parse(["--host-create", "example.com", "-i", "c", "-p", "80"])
        self.assertEqual(args.scheme, "http")
        self.assertFalse(args.websocket)
        self.assertFalse(args.no_ssl_force)
        self.assertFalse(args.no_http2)
        self.assertFalse(args.hsts)
        self.assertFalse(args.caching)
        self.assertFalse(args.no_block_exploits)
        self.assertEqual(args.access_list_id, 0)

    def test_host_create_all_flags(self):
        args = self._parse([
            "--host-create", "app.example.com",
            "-i", "appcontainer", "-p", "3000",
            "--scheme", "https",
            "--websocket",
            "--no-ssl-force",
            "--no-http2",
            "--hsts",
            "--caching",
            "--no-block-exploits",
            "--access-list-id", "5",
            "--advanced-config", "client_max_body_size 50M;",
        ])
        self.assertEqual(args.scheme, "https")
        self.assertTrue(args.websocket)
        self.assertTrue(args.no_ssl_force)
        self.assertTrue(args.no_http2)
        self.assertTrue(args.hsts)
        self.assertTrue(args.caching)
        self.assertTrue(args.no_block_exploits)
        self.assertEqual(args.access_list_id, 5)
        self.assertEqual(args.advanced_config, "client_max_body_size 50M;")

    def test_host_update_with_fields(self):
        args = self._parse(["--host-update", "3", "forward_port=9000", "forward_host=newcontainer"])
        self.assertEqual(args.host_update, "3")
        self.assertIn("forward_port=9000", args.fields)
        self.assertIn("forward_host=newcontainer", args.fields)

    def test_host_delete(self):
        args = self._parse(["--host-delete", "10"])
        self.assertEqual(args.host_delete, "10")

    def test_backup_flag(self):
        args = self._parse(["--backup"])
        self.assertTrue(args.backup)

    def test_backup_with_dir(self):
        args = self._parse(["--backup", "--backup-dir", "/tmp/mybackup"])
        self.assertEqual(args.backup_dir, "/tmp/mybackup")

    def test_conf_override(self):
        args = self._parse(["--info", "--conf", "/tmp/custom.conf"])
        self.assertEqual(args.conf, "/tmp/custom.conf")

    def test_mutually_exclusive_ops(self):
        with self.assertRaises(SystemExit):
            self._parse(["--info", "--host-list"])

    def test_no_op_exits(self):
        with self.assertRaises(SystemExit):
            self._parse([])


class TestFieldParsing(unittest.TestCase):

    def test_valid_kv_pairs(self):
        raw_fields = ["forward_port=9000", "forward_host=newcontainer"]
        fields = {}
        for item in raw_fields:
            k, _, v = item.partition("=")
            fields[k.strip()] = v.strip()
        self.assertEqual(fields["forward_port"], "9000")
        self.assertEqual(fields["forward_host"], "newcontainer")

    def test_missing_equals_detected(self):
        bad = "forward_portNOEQUALS"
        with self.assertRaises(SystemExit):
            if "=" not in bad:
                npm_api._die(f"Invalid field format {bad!r}; expected KEY=VALUE")

    def test_value_with_equals_in_it(self):
        item = "advanced_config=client_max_body_size=50M"
        k, _, v = item.partition("=")
        self.assertEqual(k, "advanced_config")
        self.assertEqual(v, "client_max_body_size=50M")


class TestHostUpdateSanitization(unittest.TestCase):

    def _make_client(self, tmp: Path) -> npm_api.NPMClient:
        conf = make_conf(tmp)
        cfg = npm_api.Config(conf)
        return npm_api.NPMClient(cfg)

    def test_boolean_string_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with self.assertRaises(SystemExit):
                client.host_update(1, {"ssl_forced": "true"})

    def test_bad_port_in_update_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with self.assertRaises(SystemExit):
                client.host_update(1, {"forward_port": "notaport"})

    def test_bad_scheme_in_update_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with self.assertRaises(SystemExit):
                client.host_update(1, {"forward_scheme": "ftp"})

    def test_bad_host_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with self.assertRaises(SystemExit):
                client.host_update(0, {"forward_port": 80})

    def test_bad_access_list_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with self.assertRaises(SystemExit):
                client.host_update(1, {"access_list_id": "not-an-id"})

    def test_access_list_id_zero_clears_value(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            captured = {}

            def fake_request(method, path, body=None):
                captured.update(body or {})
                return captured

            client.request = fake_request
            result = client.host_update(1, {"access_list_id": "0"})
            self.assertIsNone(result["access_list_id"])


class TestBackup(unittest.TestCase):

    def _make_client_with_mock_request(self, tmp: Path) -> npm_api.NPMClient:
        conf = make_conf(tmp)
        cfg = npm_api.Config(conf)
        client = npm_api.NPMClient(cfg)
        client._token = "faketoken"
        return client

    def test_backup_creates_timestamped_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._make_client_with_mock_request(tmp)
            backup_out = tmp / "backups"

            fake_data = {
                "/nginx/proxy-hosts": [{"id": 1}],
                "/users": [{"id": 1}],
                "/settings": {"key": "val"},
                "/nginx/access-lists": [],
                "/nginx/certificates": [],
            }

            def fake_request(method, path, body=None):
                return fake_data.get(path, [])

            client.request = fake_request
            out = client.backup(backup_dir=backup_out)

            self.assertTrue(out.exists())
            self.assertTrue((out / "proxy_hosts.json").exists())
            self.assertTrue((out / "users.json").exists())
            self.assertTrue((out / "settings.json").exists())
            self.assertTrue((out / "manifest.json").exists())

    def test_backup_manifest_structure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._make_client_with_mock_request(tmp)
            backup_out = tmp / "backups"

            def fake_request(method, path, body=None):
                return [{"id": 1}, {"id": 2}] if "proxy-hosts" in path else []

            client.request = fake_request
            out = client.backup(backup_dir=backup_out)

            manifest = json.loads((out / "manifest.json").read_text())
            self.assertIn("backup_timestamp", manifest)
            self.assertIn("items", manifest)

    def test_backup_json_files_chmod_600(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._make_client_with_mock_request(tmp)
            backup_out = tmp / "backups"

            def fake_request(method, path, body=None):
                return []

            client.request = fake_request
            out = client.backup(backup_dir=backup_out)

            for f in out.iterdir():
                mode = oct(f.stat().st_mode)[-3:]
                self.assertEqual(mode, "600", f"{f} should be chmod 600, got {mode}")

    def test_backup_partial_failure_continues(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._make_client_with_mock_request(tmp)
            backup_out = tmp / "backups"

            call_count = [0]

            def fake_request(method, path, body=None):
                call_count[0] += 1
                if "access-lists" in path:
                    npm_api._die("Simulated API failure for access-lists")
                return []

            client.request = fake_request
            out = client.backup(backup_dir=backup_out)
            self.assertTrue(out.exists())
            self.assertTrue((out / "proxy_hosts.json").exists())

    def test_backup_dir_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            client = self._make_client_with_mock_request(tmp)
            backup_out = tmp / "backups"

            def fake_request(method, path, body=None):
                return []

            client.request = fake_request
            out = client.backup(backup_dir=backup_out)
            mode = oct(out.stat().st_mode)[-3:]
            self.assertEqual(mode, "700")


class TestHTTPErrorHandling(unittest.TestCase):

    def _make_client(self, tmp: Path) -> npm_api.NPMClient:
        conf = make_conf(tmp)
        cfg = npm_api.Config(conf)
        client = npm_api.NPMClient(cfg)
        client._token = "faketoken"
        return client

    def test_http_error_dies_with_message(self):
        import requests as req_lib
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.json.return_value = {"error": {"message": "Host not found"}}
            mock_resp.text = json.dumps({"error": {"message": "Host not found"}})
            http_error = req_lib.HTTPError(response=mock_resp)
            mock_resp.raise_for_status.side_effect = http_error

            with patch.object(client._session, "request", return_value=mock_resp):
                with self.assertRaises(SystemExit):
                    client._raw_request("GET", "/nginx/proxy-hosts/999", auth_token="faketoken")

    def test_network_error_dies_with_message(self):
        import requests as req_lib
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            with patch.object(
                client._session, "request",
                side_effect=req_lib.ConnectionError("Connection refused"),
            ):
                with self.assertRaises(SystemExit):
                    client._raw_request("GET", "/nginx/proxy-hosts", auth_token="faketoken")

    def test_invalid_json_response_dies(self):
        import requests as req_lib
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status.return_value = None
            mock_resp.content = b"not json at all {"
            mock_resp.text = "not json at all {"
            mock_resp.json.side_effect = req_lib.exceptions.JSONDecodeError("err", "doc", 0)

            with patch.object(client._session, "request", return_value=mock_resp):
                with self.assertRaises(SystemExit):
                    client._raw_request("GET", "/nginx/proxy-hosts", auth_token="faketoken")

    def test_empty_response_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            mock_resp = MagicMock()
            mock_resp.status_code = 204
            mock_resp.raise_for_status.return_value = None
            mock_resp.content = b""

            with patch.object(client._session, "request", return_value=mock_resp):
                result = client._raw_request("DELETE", "/nginx/proxy-hosts/1", auth_token="faketoken")
                self.assertEqual(result, {})


class TestDebugDiagnostics(unittest.TestCase):

    def _make_client(self, tmp: Path) -> npm_api.NPMClient:
        conf = make_conf(
            tmp,
            API_USER="debug-user-sentinel",
            API_PASS="debug-password-sentinel",
        )
        return npm_api.NPMClient(npm_api.Config(conf))

    def test_request_debug_is_useful_without_logging_sensitive_values(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            response_body = "debug-response-body-sentinel"
            request_body = {
                "identity": "debug-user-sentinel",
                "secret": "debug-password-sentinel",
                "payload": "debug-request-body-sentinel",
            }
            bearer_token = "debug-bearer-token-sentinel"
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.content = response_body.encode()
            mock_resp.text = response_body
            mock_resp.json.return_value = {"result": response_body}
            stderr = StringIO()

            with (
                patch.object(client._session, "request", return_value=mock_resp) as request,
                patch.object(npm_api_support, "DEBUG", True),
                patch("sys.stderr", stderr),
            ):
                client._raw_request(
                    "POST",
                    "/tokens?expiry=24h",
                    request_body,
                    auth_token=bearer_token,
                )

            debug_output = stderr.getvalue()
            self.assertIn("[npm_api DEBUG] POST http://127.0.0.1:81/api/tokens?expiry=24h", debug_output)
            for sensitive in (
                "debug-user-sentinel",
                "debug-password-sentinel",
                "debug-request-body-sentinel",
                response_body,
                bearer_token,
                "Authorization",
                "Bearer",
            ):
                self.assertNotIn(sensitive, debug_output)

            _, _, kwargs = request.mock_calls[0]
            self.assertEqual(kwargs["json"], request_body)
            self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {bearer_token}"})

    def test_token_refresh_debug_omits_credentials_and_token_values(self):
        with tempfile.TemporaryDirectory() as td:
            client = self._make_client(Path(td))
            short_token = "debug-short-token-sentinel"
            long_token = "debug-long-token-sentinel"
            expiry = "2030-01-01T00:00:00Z"
            stderr = StringIO()

            with (
                patch.object(
                    client,
                    "_raw_request",
                    side_effect=[
                        {"token": short_token},
                        {"token": long_token, "expires": expiry},
                    ],
                ),
                patch.object(npm_api_support, "DEBUG", True),
                patch("sys.stderr", stderr),
            ):
                client._refresh_token()

            debug_output = stderr.getvalue()
            self.assertIn("[npm_api DEBUG] Requesting new API token", debug_output)
            self.assertIn(f"[npm_api DEBUG] Token cached; expires {expiry}", debug_output)
            for sensitive in (
                "debug-user-sentinel",
                "debug-password-sentinel",
                short_token,
                long_token,
            ):
                self.assertNotIn(sensitive, debug_output)


class TestHelp(unittest.TestCase):

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new_callable=StringIO):
                npm_api._build_parser().parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
