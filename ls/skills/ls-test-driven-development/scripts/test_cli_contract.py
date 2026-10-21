"""Skill-local tests for the TDD guide script contract."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coverage_analyzer import CoverageAnalyzer
from fixture_generator import FixtureGenerator
from format_detector import FormatDetector
from framework_adapter import Framework, FrameworkAdapter, Language
from metrics_calculator import MetricsCalculator
from tdd_workflow import TDDWorkflow
from test_generator import (
    TestFramework as GeneratorFramework,
    TestGenerator as Generator,
    TestType as GeneratorTestType,
)


SCRIPT_DIR = Path(__file__).resolve().parent


class CliContractTests(unittest.TestCase):
    """Verify documented CLI and critical library behavior."""

    def run_script(self, script_name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / script_name), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_advertised_scripts_have_help(self) -> None:
        for script_name in [
            "coverage_analyzer.py",
            "fixture_generator.py",
            "format_detector.py",
            "framework_adapter.py",
            "metrics_calculator.py",
            "output_formatter.py",
            "tdd_workflow.py",
            "test_generator.py",
        ]:
            with self.subTest(script=script_name):
                result = self.run_script(script_name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_format_detector_cli_detects_python_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write("import os\n\n\ndef test_example():\n    assert True\n")
            path = handle.name
        try:
            result = self.run_script("format_detector.py", "--file", path)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["language"], "python")
            self.assertEqual(payload["framework"], "pytest")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_coverage_invalid_json_reports_error(self) -> None:
        analyzer = CoverageAnalyzer()
        with self.assertRaises(ValueError):
            analyzer.detect_format("{not json")

    def test_format_detector_does_not_swallow_json_parse_as_success(self) -> None:
        detector = FormatDetector()
        self.assertEqual(detector.detect_coverage_format("{not json"), "unknown")

    def test_yaml_fixture_uses_pyyaml_semantics(self) -> None:
        content = FixtureGenerator().generate_fixture_file(
            "sample",
            {"name": "Ada Lovelace", "items": ["alpha", "beta"]},
            "yaml",
        )
        self.assertIn("name: Ada Lovelace", content)
        self.assertIn("- alpha", content)

    def test_refactor_quality_can_fail(self) -> None:
        workflow = TDDWorkflow()
        self.assertFalse(workflow._check_quality_improvement("x = 1\n", "x = 1\n"))

    def test_cobertura_preserves_branch_totals_and_branchless_state(self) -> None:
        report = """<coverage><packages><package><classes>
<class filename="a.py"><lines><line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/></lines></class>
<class filename="b.py"><lines><line number="1" hits="1"/></lines></class>
</classes></package></packages></coverage>"""
        analyzer = CoverageAnalyzer()
        data = analyzer.parse_coverage_report(report, "xml")
        self.assertEqual(data["a.py"]["branch_counts"], {"covered": 1, "total": 2})
        summary = analyzer.calculate_summary()
        self.assertEqual(summary["branch_coverage"], 50.0)
        gaps = analyzer.identify_gaps(80)
        self.assertEqual([gap["file"] for gap in gaps], ["a.py"])
        self.assertIsNone(analyzer.get_file_coverage("b.py")["branch_coverage"])

    def test_jacoco_report_is_detected_and_parsed(self) -> None:
        report = """<report><package name="example"><sourcefile name="Thing.java">
<line nr="10" mi="1" ci="0" mb="1" cb="1"/>
<line nr="11" mi="0" ci="2" mb="0" cb="0"/>
</sourcefile></package></report>"""
        analyzer = CoverageAnalyzer()
        self.assertEqual(analyzer.detect_format(report), "xml")
        data = analyzer.parse_coverage_report(report, "xml")
        self.assertEqual(data["example/Thing.java"]["branch_counts"], {"covered": 1, "total": 2})
        self.assertEqual(analyzer.calculate_summary()["line_coverage"], 50.0)

    def test_coverage_threshold_rejects_out_of_range_input(self) -> None:
        analyzer = CoverageAnalyzer()
        analyzer.coverage_data = {"a.py": {"lines": {1: 1}, "branches": {}}}
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            analyzer.identify_gaps(101)

    def test_framework_adapter_implements_jasmine_and_standalone_pytest(self) -> None:
        jasmine = FrameworkAdapter(Framework.JASMINE, Language.JAVASCRIPT)
        self.assertIn("Jasmine", jasmine.generate_imports())
        self.assertIn("describe", jasmine.generate_test_suite_wrapper("suite", "it('x', () => {});"))
        self.assertIn("it(", jasmine.generate_test_function("works", "expect(true).toBe(true);"))
        self.assertIn("expect(result).toBe(expected)", jasmine.generate_assertion("result", "expected"))
        self.assertIn("beforeEach", jasmine.generate_setup_teardown("reset();"))

        pytest_adapter = FrameworkAdapter(Framework.PYTEST, Language.PYTHON)
        generated = pytest_adapter.generate_test_function("works", "assert True")
        self.assertIn("def test_works():", generated)
        self.assertNotIn("(self)", generated)
        suite = pytest_adapter.generate_test_suite_wrapper("works", generated)
        self.assertNotIn("class Test", suite)
        namespace: dict[str, object] = {}
        exec(suite, namespace)
        namespace["test_works"]()
        with self.assertRaisesRegex(ValueError, "does not support"):
            FrameworkAdapter(Framework.JUNIT, Language.PYTHON)

    def test_test_type_changes_scenarios_scope_and_template(self) -> None:
        requirements = {
            "user_stories": [{
                "action": "sign_in",
                "error_conditions": [{"condition": "locked"}],
                "edge_cases": [{"scenario": "empty_password"}],
            }],
            "api_specs": [{
                "method": "POST",
                "path": "/login",
                "required_params": ["email"],
                "requires_auth": True,
            }],
        }
        generator = Generator(GeneratorFramework.PYTEST, "python")
        unit = generator.generate_from_requirements(requirements, GeneratorTestType.UNIT)
        integration = generator.generate_from_requirements(
            requirements, GeneratorTestType.INTEGRATION
        )
        e2e = generator.generate_from_requirements(requirements, GeneratorTestType.E2E)
        self.assertGreater(len(unit), len(integration))
        self.assertGreater(len(integration), len(e2e))
        self.assertEqual({case["execution_scope"] for case in e2e}, {"deployed_system"})
        self.assertIn("complete system", generator.generate_test_stub(e2e[0]))

    def test_generator_hardens_inputs_and_supports_mocha_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a list"):
            Generator(GeneratorFramework.PYTEST, "python").generate_from_requirements(
                {"user_stories": {}}, GeneratorTestType.UNIT
            )
        mocha = Generator(GeneratorFramework.MOCHA, "javascript")
        cases = mocha.generate_from_requirements(
            {"acceptance_criteria": [{"id": "ready"}]},
            GeneratorTestType.INTEGRATION,
        )
        generated = mocha.generate_test_file("service", cases)
        self.assertIn("from 'mocha'", generated)
        self.assertIn("service boundary", generated)

    def test_quality_counts_real_tests_once_and_scores_to_100(self) -> None:
        calculator = MetricsCalculator()
        self.assertEqual(calculator.calculate_test_quality("")["total_tests"], 0)
        quality = calculator.calculate_test_quality(
            "def test_should_return_expected_value():\n    assert result == expected\n"
        )
        self.assertEqual(quality["total_tests"], 1)
        self.assertEqual(quality["total_assertions"], 1)
        self.assertEqual(quality["quality_score"], 100.0)

    def test_red_requires_assertion_evidence_and_optional_refactor_passes(self) -> None:
        test_code = "def test_result():\n    assert result == expected\n"
        workflow = TDDWorkflow()
        self.assertFalse(workflow.validate_red_phase(test_code)["phase_complete"])
        wrong_failure = {
            "status": "failed",
            "failure_kind": "syntax",
            "failure_message": "invalid syntax",
        }
        self.assertFalse(
            workflow.validate_red_phase(test_code, wrong_failure)["phase_complete"]
        )
        intended_failure = {
            "status": "failed",
            "failure_kind": "assertion",
            "failure_message": "expected 2, got 1",
        }
        self.assertTrue(
            workflow.validate_red_phase(test_code, intended_failure)["phase_complete"]
        )
        unchanged = workflow.validate_refactor_phase(
            "result = 1\n", "result = 1\n", {"status": "passed"}
        )
        self.assertTrue(unchanged["phase_complete"])


if __name__ == "__main__":
    unittest.main()
