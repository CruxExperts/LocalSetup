"""Generate test cases and test files from requirement inputs."""

import argparse
import json
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from cli_support import SkillCliError, emit_json, fail, read_json


class TestFramework(Enum):
    """Supported testing frameworks."""
    JEST = "jest"
    VITEST = "vitest"
    PYTEST = "pytest"
    JUNIT = "junit"
    MOCHA = "mocha"


class TestType(Enum):
    """Types of tests to generate."""
    UNIT = "unit"
    INTEGRATION = "integration"
    E2E = "e2e"


class TestGenerator:
    """Generate test cases and test stubs from requirements and code."""

    def __init__(self, framework: TestFramework, language: str):
        if not isinstance(language, str) or not language.strip():
            raise ValueError("Language must be non-empty text")
        normalized_language = language.strip().lower()
        compatible = {
            TestFramework.JEST: {'javascript', 'typescript'},
            TestFramework.VITEST: {'javascript', 'typescript'},
            TestFramework.MOCHA: {'javascript', 'typescript'},
            TestFramework.PYTEST: {'python'},
            TestFramework.JUNIT: {'java'},
        }
        if normalized_language not in compatible[framework]:
            raise ValueError(
                f"Framework {framework.value} does not support language {normalized_language}"
            )
        self.framework = framework
        self.language = normalized_language
        self.test_cases: List[Dict[str, Any]] = []

    def generate_from_requirements(
        self,
        requirements: Dict[str, Any],
        test_type: TestType = TestType.UNIT,
    ) -> List[Dict[str, Any]]:
        """Generate test-type-specific cases from validated requirements."""
        if not isinstance(requirements, dict):
            raise ValueError("Requirements JSON must be an object")
        if not isinstance(test_type, TestType):
            raise ValueError("test_type must be unit, integration, or e2e")

        test_cases: List[Dict[str, Any]] = []
        for story in self._records(requirements, 'user_stories'):
            story_cases = self._test_cases_from_story(story)
            if test_type == TestType.INTEGRATION:
                story_cases = [case for case in story_cases if case['type'] != 'edge_case']
            elif test_type == TestType.E2E:
                story_cases = [case for case in story_cases if case['type'] == 'happy_path']
            test_cases.extend(story_cases)
        for criterion in self._records(requirements, 'acceptance_criteria'):
            test_cases.extend(self._test_cases_from_criteria(criterion))
        for endpoint in self._records(requirements, 'api_specs'):
            api_cases = self._test_cases_from_api(endpoint)
            if test_type == TestType.E2E:
                api_cases = [
                    case for case in api_cases
                    if case['type'] in {'api_success', 'api_auth'}
                ]
            test_cases.extend(api_cases)

        scope = {
            TestType.UNIT: 'isolated_unit',
            TestType.INTEGRATION: 'service_boundary',
            TestType.E2E: 'deployed_system',
        }[test_type]
        for case in test_cases:
            case['test_type'] = test_type.value
            case['execution_scope'] = scope
        self.test_cases = test_cases
        return test_cases

    def _records(self, container: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        records = container.get(key, [])
        if not isinstance(records, list):
            raise ValueError(f"{key} must be a list")
        if any(not isinstance(record, dict) for record in records):
            raise ValueError(f"Every {key} entry must be an object")
        return records

    def _test_cases_from_story(self, story: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate test cases from user story."""
        test_cases = []

        # Happy path test
        test_cases.append({
            'name': f"should_{story.get('action', 'work')}_successfully",
            'type': 'happy_path',
            'description': story.get('description', ''),
            'given': story.get('given', []),
            'when': story.get('when', ''),
            'then': story.get('then', ''),
            'priority': 'P0'
        })

        # Error cases
        if 'error_conditions' in story:
            for error in self._records(story, 'error_conditions'):
                test_cases.append({
                    'name': f"should_handle_{error.get('condition', 'error')}",
                    'type': 'error_case',
                    'description': error.get('description', ''),
                    'expected_error': error.get('error_type', ''),
                    'priority': 'P0'
                })

        # Edge cases
        if 'edge_cases' in story:
            for edge_case in self._records(story, 'edge_cases'):
                test_cases.append({
                    'name': f"should_handle_{edge_case.get('scenario', 'edge_case')}",
                    'type': 'edge_case',
                    'description': edge_case.get('description', ''),
                    'priority': 'P1'
                })

        return test_cases

    def _test_cases_from_criteria(self, criterion: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate test cases from acceptance criteria."""
        return [{
            'name': f"should_meet_{criterion.get('id', 'criterion')}",
            'type': 'acceptance',
            'description': criterion.get('description', ''),
            'verification': criterion.get('verification_steps', []),
            'priority': 'P0'
        }]

    def _test_cases_from_api(self, endpoint: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate test cases from API specification."""
        test_cases = []
        method = endpoint.get('method', 'GET')
        path = endpoint.get('path', '/')
        if not isinstance(method, str) or not isinstance(path, str):
            raise ValueError("API method and path must be text")

        # Success case
        test_cases.append({
            'name': f"should_{method.lower()}_{path.replace('/', '_')}_successfully",
            'type': 'api_success',
            'method': method,
            'path': path,
            'expected_status': endpoint.get('success_status', 200),
            'priority': 'P0'
        })

        # Validation errors
        if 'required_params' in endpoint:
            test_cases.append({
                'name': f"should_return_400_for_missing_params",
                'type': 'api_validation',
                'method': method,
                'path': path,
                'expected_status': 400,
                'priority': 'P0'
            })

        # Authorization
        if endpoint.get('requires_auth', False):
            test_cases.append({
                'name': f"should_return_401_for_unauthenticated",
                'type': 'api_auth',
                'method': method,
                'path': path,
                'expected_status': 401,
                'priority': 'P0'
            })

        return test_cases

    def generate_test_stub(self, test_case: Dict[str, Any]) -> str:
        """Generate one validated, framework-specific test stub."""
        self._validate_test_case(test_case)
        generators = {
            TestFramework.JEST: self._generate_jest_stub,
            TestFramework.VITEST: self._generate_vitest_stub,
            TestFramework.PYTEST: self._generate_pytest_stub,
            TestFramework.JUNIT: self._generate_junit_stub,
            TestFramework.MOCHA: self._generate_mocha_stub,
        }
        return generators[self.framework](test_case)

    def _generate_jest_stub(self, test_case: Dict[str, Any]) -> str:
        return self._generate_javascript_stub(test_case, "toBe")

    def _generate_vitest_stub(self, test_case: Dict[str, Any]) -> str:
        return self._generate_javascript_stub(test_case, "toBe")

    def _generate_mocha_stub(self, test_case: Dict[str, Any]) -> str:
        return self._generate_javascript_stub(test_case, "to.equal")

    def _generate_javascript_stub(
        self,
        test_case: Dict[str, Any],
        matcher: str,
    ) -> str:
        name = self._js_literal(str(test_case['name']))
        description = self._comment_text(test_case.get('description', ''))
        setup, action, assertion = self._template_steps(test_case)
        return f"""describe('Feature Name', () => {{
  it({name}, () => {{
    // {description}
    // Arrange: {setup}
    // Act: {action}
    // Assert: {assertion}
    expect(true).{matcher}(true); // Replace with an observable assertion
  }});
}});"""

    def _generate_pytest_stub(self, test_case: Dict[str, Any]) -> str:
        name = self._safe_identifier(str(test_case['name']))
        description = self._comment_text(test_case.get('description', '') or name)
        setup, action, assertion = self._template_steps(test_case)
        return f'''def test_{name}():
    """{description}"""
    # Arrange: {setup}
    # Act: {action}
    # Assert: {assertion}
    assert True  # Replace with an observable assertion'''

    def _generate_junit_stub(self, test_case: Dict[str, Any]) -> str:
        method_name = self._class_suffix(str(test_case['name']))
        description = self._comment_text(test_case.get('description', ''))
        setup, action, assertion = self._template_steps(test_case)
        return f"""@Test
public void {method_name}() {{
    // {description}
    // Arrange: {setup}
    // Act: {action}
    // Assert: {assertion}
    assertTrue(true); // Replace with an observable assertion
}}"""

    def _generate_generic_stub(self, test_case: Dict[str, Any]) -> str:
        setup, action, assertion = self._template_steps(test_case)
        return (
            f"# Test: {self._comment_text(test_case['name'])}\n"
            f"# Description: {self._comment_text(test_case.get('description', ''))}\n"
            f"# Arrange: {setup}\n# Act: {action}\n# Assert: {assertion}"
        )

    def _template_steps(self, test_case: Dict[str, Any]) -> tuple[str, str, str]:
        test_type = test_case.get('test_type', TestType.UNIT.value)
        templates = {
            TestType.UNIT.value: (
                'isolate the unit and replace external collaborators',
                'call one unit behavior',
                'verify its direct result or state change',
            ),
            TestType.INTEGRATION.value: (
                'start real boundary dependencies and reset shared state',
                'exercise the service boundary',
                'verify the result and persisted or emitted effects',
            ),
            TestType.E2E.value: (
                'launch the complete system and establish a user context',
                'perform the user journey through the public interface',
                'verify the user-visible outcome',
            ),
        }
        if test_type not in templates:
            raise ValueError("Test case test_type must be unit, integration, or e2e")
        return templates[test_type]

    def _validate_test_case(self, test_case: Dict[str, Any]) -> None:
        if not isinstance(test_case, dict):
            raise ValueError("Test case must be an object")
        name = test_case.get('name')
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Test case name must be non-empty text")

    def _safe_identifier(self, text: str) -> str:
        identifier = re.sub(r'[^A-Za-z0-9_]+', '_', text).strip('_').lower()
        if not identifier:
            return 'generated'
        return f"case_{identifier}" if identifier[0].isdigit() else identifier

    def _class_suffix(self, text: str) -> str:
        words = re.findall(r'[A-Za-z0-9]+', text)
        return ''.join(word.capitalize() for word in words) or 'Generated'

    def _js_literal(self, text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    def _comment_text(self, value: Any) -> str:
        return ' '.join(str(value).replace('*/', '* /').splitlines()).strip()

    def generate_test_file(
        self,
        module_name: str,
        test_cases: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Generate a complete file for every advertised framework choice."""
        module_path = self._validated_module_name(module_name)
        cases = self.test_cases if test_cases is None else test_cases
        if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
            raise ValueError("test_cases must be a list of objects")
        generators = {
            TestFramework.JEST: self._generate_jest_file,
            TestFramework.VITEST: self._generate_vitest_file,
            TestFramework.PYTEST: self._generate_pytest_file,
            TestFramework.JUNIT: self._generate_junit_file,
            TestFramework.MOCHA: self._generate_mocha_file,
        }
        return generators[self.framework](module_path, cases)

    def _generate_jest_file(self, module_name: str, test_cases: List[Dict[str, Any]]) -> str:
        imports = f"import * as moduleUnderTest from '../{module_name}';\n\n"
        return imports + self._render_stubs(test_cases, self._generate_jest_stub, "\n\n")

    def _generate_pytest_file(self, module_name: str, test_cases: List[Dict[str, Any]]) -> str:
        imports = (
            "import importlib\n\n"
            f"module_under_test = importlib.import_module({module_name!r})\n\n\n"
        )
        return imports + self._render_stubs(test_cases, self._generate_pytest_stub, "\n\n\n")

    def _generate_junit_file(self, module_name: str, test_cases: List[Dict[str, Any]]) -> str:
        class_name = self._class_suffix(module_name)
        imports = """import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

"""
        stubs = self._render_stubs(test_cases, self._generate_junit_stub, "\n\n")
        return imports + f"public class {class_name}Test {{\n\n{stubs}\n}}"

    def _generate_vitest_file(self, module_name: str, test_cases: List[Dict[str, Any]]) -> str:
        imports = (
            "import { describe, it, expect } from 'vitest';\n"
            f"import * as moduleUnderTest from '../{module_name}';\n\n"
        )
        return imports + self._render_stubs(test_cases, self._generate_vitest_stub, "\n\n")

    def _generate_mocha_file(self, module_name: str, test_cases: List[Dict[str, Any]]) -> str:
        imports = (
            "import { describe, it } from 'mocha';\n"
            "import { expect } from 'chai';\n"
            f"import * as moduleUnderTest from '../{module_name}';\n\n"
        )
        return imports + self._render_stubs(test_cases, self._generate_mocha_stub, "\n\n")

    def _render_stubs(self, test_cases, renderer, separator: str) -> str:
        stubs = []
        for test_case in test_cases:
            self._validate_test_case(test_case)
            stubs.append(renderer(test_case))
        return separator.join(stubs)

    def _validated_module_name(self, module_name: str) -> str:
        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError("Module name must be non-empty text")
        normalized = module_name.strip()
        if (
            not re.fullmatch(r'[A-Za-z0-9_./-]+', normalized)
            or normalized.startswith('/')
            or '..' in normalized
        ):
            raise ValueError("Module name contains unsafe path or identifier characters")
        return normalized

    def suggest_missing_scenarios(
        self,
        existing_tests: List[str],
        code_analysis: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Suggest uncovered error, branch, and boundary scenarios."""
        if not isinstance(existing_tests, list) or any(
            not isinstance(test, str) for test in existing_tests
        ):
            raise ValueError("existing_tests must be a list of names")
        if not isinstance(code_analysis, dict):
            raise ValueError("code_analysis must be an object")
        suggestions = []
        definitions = [
            ('error_handlers', 'type', 'should_handle_{}', 'error_case',
             'Error handler exists but no corresponding test', 'P0'),
            ('conditional_branches', 'condition', 'should_test_{}_branch', 'branch_coverage',
             'Conditional branch not fully tested', 'P1'),
            ('input_validation', 'parameter', 'should_test_{}_boundary_values', 'boundary',
             'Input validation exists but boundary tests missing', 'P1'),
        ]
        for key, field, template, scenario_type, reason, priority in definitions:
            for item in self._records(code_analysis, key):
                keyword = str(item.get(field, field))
                lookup = f"{keyword}_boundary" if key == 'input_validation' else keyword
                if not self._has_test_for(existing_tests, lookup):
                    suggestions.append({
                        'name': template.format(keyword),
                        'type': scenario_type,
                        'reason': reason,
                        'priority': priority,
                    })
        return suggestions

    def _has_test_for(self, existing_tests: List[str], keyword: str) -> bool:
        """Check if existing tests cover a keyword/scenario."""
        keyword_lower = keyword.lower().replace('_', '').replace('-', '')
        for test in existing_tests:
            test_lower = test.lower().replace('_', '').replace('-', '')
            if keyword_lower in test_lower:
                return True
        return False


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Generate test case specs or test stubs from requirements JSON."
    )
    parser.add_argument("--input", help="Path to requirements JSON.")
    parser.add_argument("--input-json", help="Inline requirements JSON.")
    parser.add_argument(
        "--framework",
        choices=[framework.value for framework in TestFramework],
        default=TestFramework.PYTEST.value,
        help="Target test framework.",
    )
    parser.add_argument(
        "--language",
        default="python",
        help="Target language, such as python, javascript, typescript, or java.",
    )
    parser.add_argument(
        "--test-type",
        choices=[test_type.value for test_type in TestType],
        default=TestType.UNIT.value,
    )
    parser.add_argument("--module", help="Generate a complete test file for this module.")
    parser.add_argument(
        "--existing-tests-json",
        help="Inline JSON list of existing test names for scenario suggestions.",
    )
    parser.add_argument(
        "--code-analysis-json",
        help="Inline code-analysis JSON for scenario suggestions.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        generator = TestGenerator(TestFramework(args.framework), args.language)

        if args.existing_tests_json or args.code_analysis_json:
            existing = read_json(inline=args.existing_tests_json or "[]")
            analysis = read_json(inline=args.code_analysis_json or "{}")
            emit_json(generator.suggest_missing_scenarios(existing, analysis))
            return 0

        requirements = read_json(args.input, args.input_json)
        cases = generator.generate_from_requirements(
            requirements, TestType(args.test_type)
        )
        if args.module:
            print(generator.generate_test_file(args.module, cases))
        else:
            emit_json({"test_cases": cases})
        return 0
    except (SkillCliError, ValueError, KeyError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
