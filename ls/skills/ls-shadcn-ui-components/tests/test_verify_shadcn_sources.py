from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
SKILL_ROOT = ROOT / "ls" / "skills" / "ls-shadcn-ui-components"
VERIFIER = SKILL_ROOT / "scripts" / "verify_shadcn_sources.py"


def load_verifier() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("verify_shadcn_sources", VERIFIER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load verifier module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerifyShadcnSourcesTests(unittest.TestCase):
    def run_without_site_packages(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-S", str(VERIFIER), *args],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_help_and_offline_checks_do_not_require_requests(self) -> None:
        help_result = self.run_without_site_packages("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)

        text_result = self.run_without_site_packages()
        self.assertEqual(text_result.returncode, 0, text_result.stderr)
        self.assertIn("static: ok", text_result.stdout)

        json_result = self.run_without_site_packages("--json")
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        payload = json.loads(json_result.stdout)
        self.assertTrue(payload["static_ok"])
        self.assertEqual(payload["static_errors"], [])
        self.assertIsNone(payload["refresh_ok"])
        self.assertEqual(payload["refresh_errors"], [])
        self.assertTrue(payload["ok"])

    def test_refresh_failures_do_not_contaminate_static_status(self) -> None:
        verifier = load_verifier()

        class FakeRequestException(Exception):
            pass

        fake_requests = types.SimpleNamespace(RequestException=FakeRequestException)
        failed_url = {"url": verifier.OFFICIAL_URLS[0], "ok": False, "status": 503}
        npm_payload = {
            "dist-tags": {"latest": "0.0.0"},
            "time": {"0.0.0": "2000-01-01T00:00:00.000Z"},
        }
        output = io.StringIO()
        with (
            patch.object(verifier, "load_requests", return_value=fake_requests),
            patch.object(verifier, "fetch_url", return_value=failed_url),
            patch.object(verifier, "fetch_json", return_value=npm_payload),
            redirect_stdout(output),
        ):
            status = verifier.main(["--refresh", "--json"])

        payload = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertTrue(payload["static_ok"])
        self.assertEqual(payload["static_errors"], [])
        self.assertFalse(payload["refresh_ok"])
        self.assertTrue(payload["refresh_errors"])
        self.assertFalse(payload["ok"])

    def test_repository_validation_commands_are_root_relative(self) -> None:
        command = "python ls/skills/ls-shadcn-ui-components/scripts/verify_shadcn_sources.py"
        checklist = (SKILL_ROOT / "tests" / "validation-checklist.md").read_text(encoding="utf-8")
        self.assertIn(command, checklist)
        self.assertIn("Run this entire checklist from the LocalSetup repository root.", checklist)


if __name__ == "__main__":
    unittest.main()
