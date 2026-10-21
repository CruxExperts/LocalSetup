"""
Fixture and test data generation module.

Generates realistic test data, mock objects, and fixtures for various scenarios.
"""

from typing import Dict, List, Any, Optional
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from deps import require_deps  # noqa: E402

require_deps(["yaml"])
import yaml  # noqa: E402

from cli_support import SkillCliError, emit_json, fail, read_json


class FixtureGenerator:
    """Generate test fixtures and mock data."""

    def __init__(self, seed: Optional[int] = None):
        """
        Initialize fixture generator.

        Args:
            seed: Random seed for reproducible fixtures
        """
        if seed is not None:
            random.seed(seed)

    def generate_boundary_values(
        self,
        data_type: str,
        constraints: Optional[Dict[str, Any]] = None
    ) -> List[Any]:
        """
        Generate boundary values for testing.

        Args:
            data_type: Type of data (int, string, array, date, etc.)
            constraints: Constraints like min, max, length

        Returns:
            List of boundary values
        """
        constraints = constraints or {}

        if data_type == "int":
            return self._integer_boundaries(constraints)
        elif data_type == "string":
            return self._string_boundaries(constraints)
        elif data_type == "array":
            return self._array_boundaries(constraints)
        elif data_type == "date":
            return self._date_boundaries(constraints)
        elif data_type == "email":
            return self._email_boundaries()
        elif data_type == "url":
            return self._url_boundaries()
        else:
            return []

    def _integer_boundaries(self, constraints: Dict[str, Any]) -> List[int]:
        """Generate integer boundary values."""
        min_val = constraints.get('min', 0)
        max_val = constraints.get('max', 100)

        boundaries = [
            min_val,  # Minimum
            min_val + 1,  # Just above minimum
            max_val - 1,  # Just below maximum
            max_val,  # Maximum
        ]

        # Add special values
        if min_val <= 0 <= max_val:
            boundaries.append(0)  # Zero
        if min_val < 0:
            boundaries.append(-1)  # Negative

        return sorted(set(boundaries))

    def _string_boundaries(self, constraints: Dict[str, Any]) -> List[str]:
        """Generate string boundary values."""
        min_len = constraints.get('min_length', 0)
        max_len = constraints.get('max_length', 100)

        boundaries = [
            "",  # Empty string
            "a" * min_len,  # Minimum length
            "a" * (min_len + 1) if min_len < max_len else "",  # Just above minimum
            "a" * (max_len - 1) if max_len > 1 else "a",  # Just below maximum
            "a" * max_len,  # Maximum length
            "a" * (max_len + 1),  # Exceeds maximum (invalid)
        ]

        # Add special characters
        if max_len >= 10:
            boundaries.append("test@#$%^&*()")  # Special characters
            boundaries.append("unicode: 你好")  # Unicode

        return [b for b in boundaries if b is not None]

    def _array_boundaries(self, constraints: Dict[str, Any]) -> List[List[Any]]:
        """Generate array boundary values."""
        min_size = constraints.get('min_size', 0)
        max_size = constraints.get('max_size', 10)

        boundaries = [
            [],  # Empty array
            [1] * min_size,  # Minimum size
            [1] * max_size,  # Maximum size
            [1] * (max_size + 1),  # Exceeds maximum (invalid)
        ]

        return boundaries

    def _date_boundaries(self, constraints: Dict[str, Any]) -> List[str]:
        """Generate date boundary values."""
        return [
            "1900-01-01",  # Very old date
            "1970-01-01",  # Unix epoch
            "2000-01-01",  # Y2K
            "2025-11-05",  # Fixed reference date
            "2099-12-31",  # Far future
            "invalid-date",  # Invalid format
        ]

    def _email_boundaries(self) -> List[str]:
        """Generate email boundary values."""
        return [
            "valid@example.com",  # Valid
            "user.name+tag@example.co.uk",  # Valid with special chars
            "invalid",  # Missing @
            "@example.com",  # Missing local part
            "user@",  # Missing domain
            "user@.com",  # Invalid domain
            "",  # Empty
        ]

    def _url_boundaries(self) -> List[str]:
        """Generate URL boundary values."""
        return [
            "https://example.com",  # Valid HTTPS
            "http://example.com",  # Valid HTTP
            "ftp://example.com",  # Different protocol
            "//example.com",  # Protocol-relative
            "example.com",  # Missing protocol
            "",  # Empty
            "not a url",  # Invalid
        ]

    def generate_edge_cases(
        self,
        scenario: str,
        context: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Generate edge case test scenarios.

        Args:
            scenario: Type of scenario (auth, payment, form, api, etc.)
            context: Additional context for scenario

        Returns:
            List of edge case test scenarios
        """
        if scenario == "auth":
            return self._auth_edge_cases()
        elif scenario == "payment":
            return self._payment_edge_cases()
        elif scenario == "form":
            return self._form_edge_cases(context or {})
        elif scenario == "api":
            return self._api_edge_cases()
        elif scenario == "file_upload":
            return self._file_upload_edge_cases()
        else:
            return []

    def _auth_edge_cases(self) -> List[Dict[str, Any]]:
        """Generate authentication edge cases."""
        return [
            {
                'name': 'empty_credentials',
                'input': {'username': '', 'password': ''},
                'expected': 'validation_error'
            },
            {
                'name': 'sql_injection_attempt',
                'input': {'username': "admin' OR '1'='1", 'password': 'password'},
                'expected': 'authentication_failed'
            },
            {
                'name': 'very_long_password',
                'input': {'username': 'user', 'password': 'a' * 1000},
                'expected': 'validation_error_or_success'
            },
            {
                'name': 'special_chars_username',
                'input': {'username': 'user@#$%', 'password': 'password'},
                'expected': 'depends_on_validation'
            },
            {
                'name': 'unicode_credentials',
                'input': {'username': '用户', 'password': 'пароль'},
                'expected': 'should_handle_unicode'
            }
        ]

    def _payment_edge_cases(self) -> List[Dict[str, Any]]:
        """Generate payment processing edge cases."""
        return [
            {
                'name': 'zero_amount',
                'input': {'amount': 0, 'currency': 'USD'},
                'expected': 'validation_error'
            },
            {
                'name': 'negative_amount',
                'input': {'amount': -10, 'currency': 'USD'},
                'expected': 'validation_error'
            },
            {
                'name': 'very_large_amount',
                'input': {'amount': 999999999.99, 'currency': 'USD'},
                'expected': 'should_handle_or_reject'
            },
            {
                'name': 'precision_test',
                'input': {'amount': 10.999, 'currency': 'USD'},
                'expected': 'should_round_to_10.99'
            },
            {
                'name': 'invalid_currency',
                'input': {'amount': 10, 'currency': 'XXX'},
                'expected': 'validation_error'
            }
        ]

    def _form_edge_cases(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate form validation edge cases."""
        fields = context.get('fields', [])
        edge_cases = []

        for field in fields:
            field_name = field.get('name', 'field')
            field_type = field.get('type', 'text')

            edge_cases.append({
                'name': f'{field_name}_empty',
                'input': {field_name: ''},
                'expected': 'validation_error_if_required'
            })

            if field_type in ['text', 'email', 'password']:
                edge_cases.append({
                    'name': f'{field_name}_very_long',
                    'input': {field_name: 'a' * 1000},
                    'expected': 'validation_error_or_truncate'
                })

        return edge_cases

    def _api_edge_cases(self) -> List[Dict[str, Any]]:
        """Generate API edge cases."""
        return [
            {
                'name': 'missing_required_field',
                'request': {'optional_field': 'value'},
                'expected': 400
            },
            {
                'name': 'invalid_json',
                'request': 'not valid json{',
                'expected': 400
            },
            {
                'name': 'empty_body',
                'request': {},
                'expected': 400
            },
            {
                'name': 'very_large_payload',
                'request': {'data': 'x' * 1000000},
                'expected': '413_or_400'
            },
            {
                'name': 'invalid_method',
                'method': 'INVALID',
                'expected': 405
            }
        ]

    def _file_upload_edge_cases(self) -> List[Dict[str, Any]]:
        """Generate file upload edge cases."""
        return [
            {
                'name': 'empty_file',
                'file': {'name': 'test.txt', 'size': 0},
                'expected': 'validation_error'
            },
            {
                'name': 'very_large_file',
                'file': {'name': 'test.txt', 'size': 1000000000},
                'expected': 'size_limit_error'
            },
            {
                'name': 'invalid_extension',
                'file': {'name': 'test.exe', 'size': 1000},
                'expected': 'validation_error'
            },
            {
                'name': 'no_extension',
                'file': {'name': 'testfile', 'size': 1000},
                'expected': 'depends_on_validation'
            },
            {
                'name': 'special_chars_filename',
                'file': {'name': 'test@#$%.txt', 'size': 1000},
                'expected': 'should_sanitize'
            }
        ]

    def generate_mock_data(
        self,
        schema: Dict[str, Any],
        count: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Generate mock data based on schema.

        Args:
            schema: Schema definition with field types
            count: Number of mock objects to generate

        Returns:
            List of mock data objects
        """
        mock_objects = []

        for _ in range(count):
            mock_obj = {}

            for field_name, field_def in schema.items():
                field_type = field_def.get('type', 'string')
                mock_obj[field_name] = self._generate_field_value(field_type, field_def)

            mock_objects.append(mock_obj)

        return mock_objects

    def _generate_field_value(self, field_type: str, field_def: Dict[str, Any]) -> Any:
        """Generate value for a single field."""
        if field_type == "string":
            options = field_def.get('options')
            if options:
                return random.choice(options)
            return f"test_string_{random.randint(1, 1000)}"

        elif field_type == "int":
            min_val = field_def.get('min', 0)
            max_val = field_def.get('max', 100)
            return random.randint(min_val, max_val)

        elif field_type == "float":
            min_val = field_def.get('min', 0.0)
            max_val = field_def.get('max', 100.0)
            return round(random.uniform(min_val, max_val), 2)

        elif field_type == "bool":
            return random.choice([True, False])

        elif field_type == "email":
            return f"user{random.randint(1, 1000)}@example.com"

        elif field_type == "date":
            return f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"

        elif field_type == "array":
            item_type = field_def.get('items', {}).get('type', 'string')
            size = random.randint(1, 5)
            return [self._generate_field_value(item_type, field_def.get('items', {}))
                   for _ in range(size)]

        else:
            return None

    def generate_fixture_file(
        self,
        fixture_name: str,
        data: Any,
        format: str = "json"
    ) -> str:
        """
        Generate fixture file content.

        Args:
            fixture_name: Name of fixture
            data: Fixture data
            format: Output format (json, yaml, python)

        Returns:
            Fixture file content as string
        """
        if format == "json":
            return json.dumps(data, indent=2)

        elif format == "python":
            return f"""# {fixture_name} fixture

{fixture_name.upper()} = {repr(data)}
"""

        elif format == "yaml":
            return yaml.safe_dump(data, sort_keys=False).rstrip()

        else:
            return str(data)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Generate boundary values, edge cases, mock data, or fixture content."
    )
    parser.add_argument("--seed", type=int, help="Random seed for reproducible mock data.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    boundaries = subparsers.add_parser("boundaries", help="Generate boundary values.")
    boundaries.add_argument("--type", required=True, help="Data type, such as int or string.")
    boundaries.add_argument("--constraints-json", help="Inline constraints JSON.")
    boundaries.add_argument("--constraints-file", help="Path to constraints JSON.")

    edges = subparsers.add_parser("edge-cases", help="Generate edge-case scenarios.")
    edges.add_argument("--scenario", required=True, help="Scenario name, such as auth or api.")
    edges.add_argument("--context-json", help="Inline context JSON.")
    edges.add_argument("--context-file", help="Path to context JSON.")

    mock = subparsers.add_parser("mock-data", help="Generate mock objects from a schema.")
    mock.add_argument("--schema-json", help="Inline schema JSON.")
    mock.add_argument("--schema-file", help="Path to schema JSON.")
    mock.add_argument("--count", type=int, default=1, help="Number of objects to generate.")

    fixture = subparsers.add_parser("fixture", help="Render fixture content.")
    fixture.add_argument("--name", required=True, help="Fixture variable/name.")
    fixture.add_argument("--data-json", help="Inline data JSON.")
    fixture.add_argument("--data-file", help="Path to data JSON.")
    fixture.add_argument("--format", choices=["json", "yaml", "python"], default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    generator = FixtureGenerator(seed=args.seed)

    try:
        if args.command == "boundaries":
            constraints = read_json(args.constraints_file, args.constraints_json) if (
                args.constraints_file or args.constraints_json
            ) else {}
            emit_json(generator.generate_boundary_values(args.type, constraints))
        elif args.command == "edge-cases":
            context = read_json(args.context_file, args.context_json) if (
                args.context_file or args.context_json
            ) else {}
            emit_json(generator.generate_edge_cases(args.scenario, context))
        elif args.command == "mock-data":
            schema = read_json(args.schema_file, args.schema_json)
            emit_json(generator.generate_mock_data(schema, args.count))
        elif args.command == "fixture":
            data = read_json(args.data_file, args.data_json)
            content = generator.generate_fixture_file(args.name, data, args.format)
            print(content)
        return 0
    except (SkillCliError, RuntimeError, ValueError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
