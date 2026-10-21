"""
Framework adapter module.

Provides multi-framework support with adapters for Jest, Pytest, JUnit, Vitest, and more.
Handles framework-specific patterns, imports, and test structure.
"""

from typing import Optional
import argparse
import json
import re
from enum import Enum

from cli_support import SkillCliError, emit_json, fail, read_text


class Framework(Enum):
    """Supported testing frameworks."""
    JEST = "jest"
    VITEST = "vitest"
    PYTEST = "pytest"
    UNITTEST = "unittest"
    JUNIT = "junit"
    TESTNG = "testng"
    MOCHA = "mocha"
    JASMINE = "jasmine"


class Language(Enum):
    """Supported programming languages."""
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    PYTHON = "python"
    JAVA = "java"


class FrameworkAdapter:
    """Adapter for multiple testing frameworks."""

    def __init__(self, framework: Framework, language: Language):
        """Initialize a valid framework/language adapter."""
        compatible = {
            Framework.JEST: {Language.TYPESCRIPT, Language.JAVASCRIPT},
            Framework.VITEST: {Language.TYPESCRIPT, Language.JAVASCRIPT},
            Framework.MOCHA: {Language.TYPESCRIPT, Language.JAVASCRIPT},
            Framework.JASMINE: {Language.TYPESCRIPT, Language.JAVASCRIPT},
            Framework.PYTEST: {Language.PYTHON},
            Framework.UNITTEST: {Language.PYTHON},
            Framework.JUNIT: {Language.JAVA},
            Framework.TESTNG: {Language.JAVA},
        }
        if language not in compatible[framework]:
            raise ValueError(
                f"Framework {framework.value} does not support language {language.value}"
            )
        self.framework = framework
        self.language = language

    def generate_imports(self) -> str:
        """Generate framework-specific imports or global declarations."""
        generators = {
            Framework.JEST: self._jest_imports,
            Framework.VITEST: self._vitest_imports,
            Framework.PYTEST: self._pytest_imports,
            Framework.UNITTEST: self._unittest_imports,
            Framework.JUNIT: self._junit_imports,
            Framework.TESTNG: self._testng_imports,
            Framework.MOCHA: self._mocha_imports,
            Framework.JASMINE: self._jasmine_imports,
        }
        return generators[self.framework]()

    def _jest_imports(self) -> str:
        """Generate Jest imports."""
        return """import { describe, it, expect, beforeEach, afterEach } from '@jest/globals';"""

    def _vitest_imports(self) -> str:
        """Generate Vitest imports."""
        return """import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';"""

    def _pytest_imports(self) -> str:
        """Generate Pytest imports."""
        return """import pytest"""

    def _unittest_imports(self) -> str:
        """Generate unittest imports."""
        return """import unittest"""

    def _junit_imports(self) -> str:
        """Generate JUnit imports."""
        return """import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.AfterEach;
import static org.junit.jupiter.api.Assertions.*;"""

    def _testng_imports(self) -> str:
        """Generate TestNG imports."""
        return """import org.testng.annotations.Test;
import org.testng.annotations.BeforeMethod;
import org.testng.annotations.AfterMethod;
import static org.testng.Assert.*;"""

    def _mocha_imports(self) -> str:
        """Generate Mocha and Chai imports."""
        return """import { describe, it, beforeEach, afterEach } from 'mocha';
import { expect } from 'chai';"""

    def _jasmine_imports(self) -> str:
        """Describe Jasmine's runner-provided global API."""
        return "// Jasmine provides describe, it, expect, beforeEach, and afterEach as runner globals."

    def generate_test_suite_wrapper(
        self,
        suite_name: str,
        test_content: str
    ) -> str:
        """
        Wrap test content in framework-specific suite structure.

        Args:
            suite_name: Name of test suite
            test_content: Test functions/methods

        Returns:
            Complete test suite code
        """
        if self.framework in [
            Framework.JEST,
            Framework.VITEST,
            Framework.MOCHA,
            Framework.JASMINE,
        ]:
            return f"""describe({self._js_literal(suite_name)}, () => {{
{self._indent(test_content, 2)}
}});"""
        if self.framework == Framework.PYTEST:
            return f"""# Pytest suite: {self._comment_text(suite_name)}

{test_content}"""
        if self.framework == Framework.UNITTEST:
            return f'''class Test{self._to_class_name(suite_name)}(unittest.TestCase):
    """Test suite for {self._comment_text(suite_name)}."""

{self._indent(test_content, 4)}'''
        return f"""public class {self._to_class_name(suite_name)}Test {{

{self._indent(test_content, 4)}
}}"""

    def generate_test_function(
        self,
        test_name: str,
        test_body: str,
        description: str = "",
    ) -> str:
        """Generate one framework-specific test function."""
        if not isinstance(test_name, str) or not test_name.strip():
            raise ValueError("Test name must be non-empty text")
        if not isinstance(test_body, str) or not test_body.strip():
            raise ValueError("Test body must be non-empty text")
        generators = {
            Framework.JEST: self._jest_test,
            Framework.VITEST: self._vitest_test,
            Framework.PYTEST: self._pytest_test,
            Framework.UNITTEST: self._unittest_test,
            Framework.JUNIT: self._junit_test,
            Framework.TESTNG: self._testng_test,
            Framework.MOCHA: self._mocha_test,
            Framework.JASMINE: self._jasmine_test,
        }
        return generators[self.framework](test_name, test_body, description)

    def _jest_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._javascript_test(test_name, test_body, description)

    def _vitest_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._javascript_test(test_name, test_body, description)

    def _mocha_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._javascript_test(test_name, test_body, description)

    def _jasmine_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._javascript_test(test_name, test_body, description)

    def _javascript_test(self, test_name: str, test_body: str, description: str) -> str:
        comment = self._comment_text(description)
        return f"""it({self._js_literal(test_name)}, () => {{
  // {comment}
{self._indent(test_body, 2)}
}});"""

    def _pytest_test(self, test_name: str, test_body: str, description: str) -> str:
        """Generate a standalone pytest function without a fixture-like self argument."""
        function_name = self._python_identifier(test_name)
        return f'''def test_{function_name}():
    """{self._comment_text(description or test_name)}"""
{self._indent(test_body, 4)}'''

    def _unittest_test(self, test_name: str, test_body: str, description: str) -> str:
        function_name = self._python_identifier(test_name)
        return f'''def test_{function_name}(self):
    """{self._comment_text(description or test_name)}"""
{self._indent(test_body, 4)}'''

    def _junit_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._java_test(test_name, test_body, description)

    def _testng_test(self, test_name: str, test_body: str, description: str) -> str:
        return self._java_test(test_name, test_body, description)

    def _java_test(self, test_name: str, test_body: str, description: str) -> str:
        method_name = self._to_class_name(test_name)
        return f"""@Test
public void test{method_name}() {{
    // {self._comment_text(description)}
{self._indent(test_body, 4)}
}}"""

    def generate_assertion(
        self,
        actual: str,
        expected: str,
        assertion_type: str = "equals"
    ) -> str:
        """
        Generate framework-specific assertion.

        Args:
            actual: Actual value expression
            expected: Expected value expression
            assertion_type: Type of assertion (equals, not_equals, true, false, throws)

        Returns:
            Assertion statement
        """
        if self.framework in [Framework.JEST, Framework.VITEST, Framework.JASMINE]:
            return self._jest_assertion(actual, expected, assertion_type)
        if self.framework in [Framework.PYTEST, Framework.UNITTEST]:
            return self._python_assertion(actual, expected, assertion_type)
        if self.framework in [Framework.JUNIT, Framework.TESTNG]:
            return self._java_assertion(actual, expected, assertion_type)
        if self.framework == Framework.MOCHA:
            return self._chai_assertion(actual, expected, assertion_type)
        raise ValueError(f"Unsupported framework: {self.framework.value}")

    def _jest_assertion(self, actual: str, expected: str, assertion_type: str) -> str:
        """Generate Jest assertion."""
        if assertion_type == "equals":
            return f"expect({actual}).toBe({expected});"
        elif assertion_type == "not_equals":
            return f"expect({actual}).not.toBe({expected});"
        elif assertion_type == "true":
            return f"expect({actual}).toBe(true);"
        elif assertion_type == "false":
            return f"expect({actual}).toBe(false);"
        elif assertion_type == "throws":
            return f"expect(() => {actual}).toThrow();"
        else:
            return f"expect({actual}).toBe({expected});"

    def _python_assertion(self, actual: str, expected: str, assertion_type: str) -> str:
        """Generate Python assertion."""
        if assertion_type == "equals":
            return f"assert {actual} == {expected}"
        elif assertion_type == "not_equals":
            return f"assert {actual} != {expected}"
        elif assertion_type == "true":
            return f"assert {actual} is True"
        elif assertion_type == "false":
            return f"assert {actual} is False"
        elif assertion_type == "throws":
            return f"with pytest.raises(Exception):\n    {actual}"
        else:
            return f"assert {actual} == {expected}"

    def _java_assertion(self, actual: str, expected: str, assertion_type: str) -> str:
        """Generate Java assertion."""
        if assertion_type == "equals":
            return f"assertEquals({expected}, {actual});"
        elif assertion_type == "not_equals":
            return f"assertNotEquals({expected}, {actual});"
        elif assertion_type == "true":
            return f"assertTrue({actual});"
        elif assertion_type == "false":
            return f"assertFalse({actual});"
        elif assertion_type == "throws":
            return f"assertThrows(Exception.class, () -> {actual});"
        else:
            return f"assertEquals({expected}, {actual});"

    def _chai_assertion(self, actual: str, expected: str, assertion_type: str) -> str:
        """Generate Chai assertion."""
        if assertion_type == "equals":
            return f"expect({actual}).to.equal({expected});"
        elif assertion_type == "not_equals":
            return f"expect({actual}).to.not.equal({expected});"
        elif assertion_type == "true":
            return f"expect({actual}).to.be.true;"
        elif assertion_type == "false":
            return f"expect({actual}).to.be.false;"
        elif assertion_type == "throws":
            return f"expect(() => {actual}).to.throw();"
        else:
            return f"expect({actual}).to.equal({expected});"

    def generate_setup_teardown(
        self,
        setup_code: str = "",
        teardown_code: str = ""
    ) -> str:
        """Generate setup and teardown hooks."""
        result = []

        if self.framework in [
            Framework.JEST,
            Framework.VITEST,
            Framework.MOCHA,
            Framework.JASMINE,
        ]:
            if setup_code:
                result.append(f"""beforeEach(() => {{
{self._indent(setup_code, 2)}
}});""")
            if teardown_code:
                result.append(f"""afterEach(() => {{
{self._indent(teardown_code, 2)}
}});""")

        elif self.framework == Framework.PYTEST:
            if setup_code:
                result.append(f"""@pytest.fixture(autouse=True)
def setup_method(self):
{self._indent(setup_code, 4)}
    yield""")
            if teardown_code:
                result.append(f"""
{self._indent(teardown_code, 4)}""")

        elif self.framework == Framework.UNITTEST:
            if setup_code:
                result.append(f"""def setUp(self):
{self._indent(setup_code, 4)}""")
            if teardown_code:
                result.append(f"""def tearDown(self):
{self._indent(teardown_code, 4)}""")

        elif self.framework in [Framework.JUNIT, Framework.TESTNG]:
            annotation = "@BeforeEach" if self.framework == Framework.JUNIT else "@BeforeMethod"
            if setup_code:
                result.append(f"""{annotation}
public void setUp() {{
{self._indent(setup_code, 4)}
}}""")

            annotation = "@AfterEach" if self.framework == Framework.JUNIT else "@AfterMethod"
            if teardown_code:
                result.append(f"""{annotation}
public void tearDown() {{
{self._indent(teardown_code, 4)}
}}""")

        return "\n\n".join(result)

    def _indent(self, text: str, spaces: int) -> str:
        """Indent text by number of spaces."""
        indent = " " * spaces
        lines = text.split('\n')
        return '\n'.join(indent + line if line.strip() else line for line in lines)

    def _to_camel_case(self, text: str) -> str:
        """Convert untrusted text to a non-empty lower camel-case identifier."""
        words = re.findall(r'[A-Za-z0-9]+', text)
        if not words:
            return 'generated'
        return words[0].lower() + ''.join(word.capitalize() for word in words[1:])

    def _to_class_name(self, text: str) -> str:
        """Convert untrusted text to a non-empty class or method suffix."""
        words = re.findall(r'[A-Za-z0-9]+', text)
        return ''.join(word.capitalize() for word in words) or 'Generated'

    def _python_identifier(self, text: str) -> str:
        identifier = re.sub(r'[^A-Za-z0-9_]+', '_', text.strip()).strip('_').lower()
        if not identifier:
            return 'generated'
        return f"case_{identifier}" if identifier[0].isdigit() else identifier

    def _js_literal(self, text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    def _comment_text(self, text: str) -> str:
        return ' '.join(str(text).replace('*/', '* /').splitlines()).strip()

    def detect_framework(self, code: str) -> Optional[Framework]:
        """Auto-detect a supported framework from non-empty test code."""
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Test code must be non-empty text")
        if "from '@jest/globals'" in code or '@jest/' in code:
            return Framework.JEST
        if "from 'vitest'" in code or 'import { vi }' in code:
            return Framework.VITEST
        if 'import pytest' in code or ('def test_' in code and 'pytest.fixture' in code):
            return Framework.PYTEST
        if 'import unittest' in code and 'unittest.TestCase' in code:
            return Framework.UNITTEST
        if '@Test' in code and 'import org.junit' in code:
            return Framework.JUNIT
        if '@Test' in code and 'import org.testng' in code:
            return Framework.TESTNG
        if "from 'mocha'" in code or ('describe(' in code and "from 'chai'" in code):
            return Framework.MOCHA
        if 'jasmine.createSpy' in code or "import 'jasmine'" in code:
            return Framework.JASMINE
        return None


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Generate framework-specific test snippets and detect test frameworks."
    )
    parser.add_argument(
        "--framework",
        choices=[framework.value for framework in Framework],
        default=Framework.PYTEST.value,
    )
    parser.add_argument(
        "--language",
        choices=[language.value for language in Language],
        default=Language.PYTHON.value,
    )
    parser.add_argument(
        "--action",
        choices=["imports", "assertion", "test-function", "suite", "detect"],
        default="imports",
    )
    parser.add_argument("--test-name", default="should handle expected behavior")
    parser.add_argument("--test-body", default="assert result == expected")
    parser.add_argument("--description", default="")
    parser.add_argument("--suite-name", default="GeneratedSuite")
    parser.add_argument("--actual", default="result")
    parser.add_argument("--expected", default="expected")
    parser.add_argument(
        "--assertion-type",
        choices=["equals", "not_equals", "true", "false", "throws"],
        default="equals",
    )
    parser.add_argument("--code-file", help="Path to test code for --action detect.")
    parser.add_argument("--code", help="Inline test code for --action detect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        adapter = FrameworkAdapter(Framework(args.framework), Language(args.language))
        if args.action == "imports":
            print(adapter.generate_imports())
        elif args.action == "assertion":
            print(adapter.generate_assertion(args.actual, args.expected, args.assertion_type))
        elif args.action == "test-function":
            print(adapter.generate_test_function(args.test_name, args.test_body, args.description))
        elif args.action == "suite":
            print(adapter.generate_test_suite_wrapper(args.suite_name, args.test_body))
        elif args.action == "detect":
            code = read_text(path=args.code_file, inline=args.code)
            detected = adapter.detect_framework(code)
            emit_json({"framework": detected.value if detected else "unknown"})
        return 0
    except (SkillCliError, ValueError, KeyError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
