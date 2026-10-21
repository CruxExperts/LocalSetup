"""
Coverage analysis module.

Parse and analyze test coverage reports in multiple formats (LCOV, JSON, XML).
Identify gaps, calculate metrics, and provide actionable recommendations.
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from cli_support import SkillCliError, emit_json, fail, read_text


class CoverageFormat:
    """Supported coverage report formats."""
    LCOV = "lcov"
    JSON = "json"
    XML = "xml"
    COBERTURA = "cobertura"


class CoverageAnalyzer:
    """Analyze test coverage reports and identify gaps."""

    def __init__(self):
        """Initialize coverage analyzer."""
        self.coverage_data = {}
        self.gaps = []
        self.summary = {}

    def parse_coverage_report(
        self,
        report_content: str,
        format_type: str,
    ) -> Dict[str, Any]:
        """Parse a non-empty LCOV, Istanbul JSON, Cobertura XML, or JaCoCo XML report."""
        if not isinstance(report_content, str) or not report_content.strip():
            raise ValueError("Coverage report must be non-empty text")
        if format_type == CoverageFormat.LCOV:
            return self._parse_lcov(report_content)
        if format_type == CoverageFormat.JSON:
            return self._parse_json(report_content)
        if format_type in [CoverageFormat.XML, CoverageFormat.COBERTURA]:
            return self._parse_xml(report_content)
        raise ValueError(f"Unsupported format: {format_type}")

    def _parse_lcov(self, content: str) -> Dict[str, Any]:
        """Parse LCOV line, function, and branch records."""
        files: Dict[str, Any] = {}
        current_file: Optional[str] = None
        file_data: Dict[str, Any] = {}

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith('SF:'):
                current_file = line[3:]
                if not current_file:
                    raise ValueError("LCOV source-file record is missing a path")
                file_data = {'lines': {}, 'functions': {}, 'branches': {}}
            elif line.startswith('DA:'):
                self._require_current_lcov_file(current_file, line)
                parts = line[3:].split(',')
                if len(parts) < 2:
                    raise ValueError(f"Invalid LCOV line record: {line}")
                file_data['lines'][int(parts[0])] = int(parts[1])
            elif line.startswith('FNDA:'):
                self._require_current_lcov_file(current_file, line)
                parts = line[5:].split(',', 1)
                if len(parts) != 2 or not parts[1]:
                    raise ValueError(f"Invalid LCOV function record: {line}")
                file_data['functions'][parts[1]] = int(parts[0])
            elif line.startswith('BRDA:'):
                self._require_current_lcov_file(current_file, line)
                parts = line[5:].split(',')
                if len(parts) != 4:
                    raise ValueError(f"Invalid LCOV branch record: {line}")
                branch_id = f"{parts[0]}:{parts[1]}:{parts[2]}"
                file_data['branches'][branch_id] = 0 if parts[3] == '-' else int(parts[3])
            elif line == 'end_of_record':
                if current_file:
                    files[current_file] = file_data
                current_file = None
                file_data = {}

        if current_file:
            files[current_file] = file_data
        if not files:
            raise ValueError("LCOV report contains no source-file records")
        self.coverage_data = files
        return files

    def _require_current_lcov_file(self, current_file: Optional[str], record: str) -> None:
        if current_file is None:
            raise ValueError(f"LCOV record appears before a source file: {record}")

    def _parse_json(self, content: str) -> Dict[str, Any]:
        """Parse an Istanbul/nyc JSON coverage report."""
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON coverage report: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON coverage report must be an object keyed by file path")

        files: Dict[str, Any] = {}
        for file_path, file_data in data.items():
            if not isinstance(file_path, str) or not isinstance(file_data, dict):
                raise ValueError("Each JSON coverage entry must map a file path to an object")
            lines: Dict[int, int] = {}
            functions: Dict[str, int] = {}
            branches: Dict[str, int] = {}
            statements = file_data.get('s', {})
            statement_map = file_data.get('statementMap', {})
            if not isinstance(statements, dict) or not isinstance(statement_map, dict):
                raise ValueError(f"Coverage entry for {file_path} has invalid statement data")
            for statement_id, hit_count in statements.items():
                statement = statement_map.get(statement_id, {})
                line_number = statement.get('start', {}).get('line') if isinstance(statement, dict) else None
                if line_number is not None:
                    lines[int(line_number)] = int(hit_count)
            function_hits = file_data.get('f', {})
            function_map = file_data.get('fnMap', {})
            if not isinstance(function_hits, dict) or not isinstance(function_map, dict):
                raise ValueError(f"Coverage entry for {file_path} has invalid function data")
            for function_id, hit_count in function_hits.items():
                function = function_map.get(function_id, {})
                name = function.get('name', f'func_{function_id}') if isinstance(function, dict) else f'func_{function_id}'
                functions[str(name)] = int(hit_count)
            branch_map = file_data.get('b', {})
            if not isinstance(branch_map, dict):
                raise ValueError(f"Coverage entry for {file_path} has invalid branch data")
            for branch_id, locations in branch_map.items():
                if not isinstance(locations, list):
                    raise ValueError(f"Coverage entry for {file_path} has invalid branch hits")
                for index, hit_count in enumerate(locations):
                    branches[f"{branch_id}:{index}"] = int(hit_count)
            files[file_path] = {
                'lines': lines,
                'functions': functions,
                'branches': branches,
            }

        self.coverage_data = files
        return files

    def _parse_xml(self, content: str) -> Dict[str, Any]:
        """Parse Cobertura or JaCoCo XML selected from the report shape."""
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML coverage report: {exc}") from exc
        root_name = root.tag.rsplit('}', 1)[-1]
        if root_name == 'report' and root.findall('.//sourcefile'):
            files = self._parse_jacoco_root(root)
        elif root_name == 'coverage' or root.findall('.//class'):
            files = self._parse_cobertura_root(root)
        else:
            raise ValueError("Unsupported XML coverage report shape")
        if not files:
            raise ValueError("XML coverage report contains no source files")
        self.coverage_data = files
        return files

    def _parse_cobertura_root(self, root: ET.Element) -> Dict[str, Any]:
        files: Dict[str, Any] = {}
        for cls in root.findall('.//class'):
            filename = cls.get('filename') or cls.get('name')
            if not filename:
                raise ValueError("Cobertura class is missing filename and name")
            lines: Dict[int, int] = {}
            branches: Dict[str, int] = {}
            covered_branches = 0
            total_branches = 0
            for line in cls.findall('lines/line'):
                line_number = self._required_nonnegative_int(line, 'number', 'Cobertura line')
                lines[line_number] = self._required_nonnegative_int(line, 'hits', 'Cobertura line')
                if line.get('branch', 'false').lower() != 'true':
                    continue
                match = re.search(r'\((\d+)\s*/\s*(\d+)\)', line.get('condition-coverage', ''))
                if not match:
                    raise ValueError(f"Cobertura branch line {line_number} has invalid condition-coverage")
                covered, total = map(int, match.groups())
                if covered > total:
                    raise ValueError(f"Cobertura branch line {line_number} covers more branches than exist")
                self._append_branch_slots(branches, str(line_number), covered, total)
                covered_branches += covered
                total_branches += total
            files[filename] = {
                'lines': lines,
                'functions': {},
                'branches': branches,
                'branch_counts': {'covered': covered_branches, 'total': total_branches},
            }
        return files

    def _parse_jacoco_root(self, root: ET.Element) -> Dict[str, Any]:
        files: Dict[str, Any] = {}
        for package in root.findall('.//package'):
            package_name = package.get('name', '').strip('/')
            for source in package.findall('sourcefile'):
                source_name = source.get('name')
                if not source_name:
                    raise ValueError("JaCoCo sourcefile is missing name")
                filename = f"{package_name}/{source_name}" if package_name else source_name
                lines: Dict[int, int] = {}
                branches: Dict[str, int] = {}
                covered_branches = 0
                total_branches = 0
                for line in source.findall('line'):
                    line_number = self._required_nonnegative_int(line, 'nr', 'JaCoCo line')
                    lines[line_number] = self._required_nonnegative_int(line, 'ci', 'JaCoCo line')
                    covered = self._required_nonnegative_int(line, 'cb', 'JaCoCo line')
                    missed = self._required_nonnegative_int(line, 'mb', 'JaCoCo line')
                    total = covered + missed
                    self._append_branch_slots(branches, str(line_number), covered, total)
                    covered_branches += covered
                    total_branches += total
                files[filename] = {
                    'lines': lines,
                    'functions': {},
                    'branches': branches,
                    'branch_counts': {'covered': covered_branches, 'total': total_branches},
                }
        return files

    def _required_nonnegative_int(
        self,
        element: ET.Element,
        attribute: str,
        context: str,
    ) -> int:
        value = element.get(attribute)
        try:
            parsed = int(value) if value is not None else -1
        except ValueError as exc:
            raise ValueError(f"{context} has invalid {attribute}: {value!r}") from exc
        if parsed < 0:
            raise ValueError(f"{context} has invalid {attribute}: {value!r}")
        return parsed

    def _append_branch_slots(
        self,
        branches: Dict[str, int],
        prefix: str,
        covered: int,
        total: int,
    ) -> None:
        for index in range(total):
            branches[f"{prefix}:branch:{index}"] = 1 if index < covered else 0

    def calculate_summary(self) -> Dict[str, Any]:
        """Calculate overall coverage, using null for non-applicable dimensions."""
        total_lines = covered_lines = 0
        total_branches = covered_branches = 0
        total_functions = covered_functions = 0

        for file_data in self.coverage_data.values():
            lines = file_data.get('lines', {})
            total_lines += len(lines)
            covered_lines += sum(1 for hit in lines.values() if hit > 0)
            branch_covered, branch_total = self._branch_totals(file_data)
            covered_branches += branch_covered
            total_branches += branch_total
            functions = file_data.get('functions', {})
            total_functions += len(functions)
            covered_functions += sum(1 for hit in functions.values() if hit > 0)

        summary = {
            'line_coverage': self._safe_percentage(covered_lines, total_lines),
            'branch_coverage': self._safe_percentage(covered_branches, total_branches),
            'function_coverage': self._safe_percentage(covered_functions, total_functions),
            'total_lines': total_lines,
            'covered_lines': covered_lines,
            'total_branches': total_branches,
            'covered_branches': covered_branches,
            'total_functions': total_functions,
            'covered_functions': covered_functions,
        }
        self.summary = summary
        return summary

    def _branch_totals(self, file_data: Dict[str, Any]) -> tuple[int, int]:
        branch_counts = file_data.get('branch_counts')
        if branch_counts is not None:
            if not isinstance(branch_counts, dict):
                raise ValueError("branch_counts must be an object")
            covered = int(branch_counts.get('covered', -1))
            total = int(branch_counts.get('total', -1))
            if covered < 0 or total < 0 or covered > total:
                raise ValueError("branch_counts must contain valid covered and total counts")
            return covered, total
        branches = file_data.get('branches', {})
        if not isinstance(branches, dict):
            raise ValueError("branches must be an object")
        return sum(1 for hit in branches.values() if hit > 0), len(branches)

    def _safe_percentage(self, covered: int, total: int) -> Optional[float]:
        """Calculate a percentage, or null when the dimension is not applicable."""
        if total == 0:
            return None
        return round((covered / total) * 100, 2)

    def identify_gaps(self, threshold: float = 80.0) -> List[Dict[str, Any]]:
        """Identify file coverage dimensions below a 0-100 threshold."""
        if not 0 <= threshold <= 100:
            raise ValueError("Coverage threshold must be between 0 and 100")
        gaps = []
        for file_path, file_data in self.coverage_data.items():
            file_gaps = self._analyze_file_gaps(file_path, file_data, threshold)
            if file_gaps:
                gaps.append(file_gaps)
        self.gaps = gaps
        return gaps

    def _analyze_file_gaps(
        self,
        file_path: str,
        file_data: Dict[str, Any],
        threshold: float,
    ) -> Optional[Dict[str, Any]]:
        """Analyze only applicable coverage dimensions for one file."""
        lines = file_data.get('lines', {})
        branches = file_data.get('branches', {})
        total_lines = len(lines)
        covered_lines = sum(1 for hit in lines.values() if hit > 0)
        line_coverage = self._safe_percentage(covered_lines, total_lines)
        covered_branches, total_branches = self._branch_totals(file_data)
        branch_coverage = self._safe_percentage(covered_branches, total_branches)
        uncovered_lines = sorted(line for line, hit in lines.items() if hit == 0)
        uncovered_branches = sorted(branch for branch, hit in branches.items() if hit == 0)
        below_threshold = [
            value
            for value in (line_coverage, branch_coverage)
            if value is not None and value < threshold
        ]
        if not below_threshold:
            return None
        return {
            'file': file_path,
            'line_coverage': line_coverage,
            'branch_coverage': branch_coverage,
            'branch_applicable': total_branches > 0,
            'uncovered_lines': uncovered_lines,
            'uncovered_branches': uncovered_branches,
            'priority': self._calculate_priority(
                line_coverage, branch_coverage, threshold
            ),
        }

    def _calculate_priority(
        self,
        line_coverage: Optional[float],
        branch_coverage: Optional[float],
        threshold: float,
    ) -> str:
        """Calculate priority from the lowest applicable coverage dimension."""
        applicable = [value for value in (line_coverage, branch_coverage) if value is not None]
        if not applicable:
            raise ValueError("Cannot prioritize a file with no applicable coverage dimensions")
        gap = threshold - min(applicable)
        if gap >= 40:
            return 'P0'
        if gap >= 20:
            return 'P1'
        return 'P2'

    def get_file_coverage(self, file_path: str) -> Dict[str, Any]:
        """Return detailed coverage for one known file."""
        if file_path not in self.coverage_data:
            return {}
        file_data = self.coverage_data[file_path]
        lines = file_data.get('lines', {})
        branches = file_data.get('branches', {})
        functions = file_data.get('functions', {})
        covered_branches, total_branches = self._branch_totals(file_data)
        return {
            'file': file_path,
            'line_coverage': self._safe_percentage(
                sum(1 for hit in lines.values() if hit > 0), len(lines)
            ),
            'branch_coverage': self._safe_percentage(covered_branches, total_branches),
            'branch_applicable': total_branches > 0,
            'function_coverage': self._safe_percentage(
                sum(1 for hit in functions.values() if hit > 0), len(functions)
            ),
            'lines': lines,
            'branches': branches,
            'functions': functions,
        }

    def generate_recommendations(self) -> List[Dict[str, Any]]:
        """Generate recommendations only for applicable coverage dimensions."""
        recommendations = []
        summary = self.summary or self.calculate_summary()
        line_coverage = summary['line_coverage']
        branch_coverage = summary['branch_coverage']
        if line_coverage is not None and line_coverage < 80:
            recommendations.append({
                'priority': 'P0',
                'type': 'overall_coverage',
                'message': f"Overall line coverage ({line_coverage}%) is below 80% threshold",
                'action': 'Focus on adding tests for critical paths and business logic',
                'impact': 'high',
            })
        if branch_coverage is not None and branch_coverage < 70:
            recommendations.append({
                'priority': 'P0',
                'type': 'branch_coverage',
                'message': f"Branch coverage ({branch_coverage}%) is below 70% threshold",
                'action': 'Add tests for conditional logic and error handling paths',
                'impact': 'high',
            })
        for gap in self.gaps:
            if gap['priority'] == 'P0':
                recommendations.append({
                    'priority': 'P0',
                    'type': 'file_coverage',
                    'file': gap['file'],
                    'message': f"Critical coverage gap in {gap['file']}",
                    'action': f"Add tests for lines: {gap['uncovered_lines'][:10]}",
                    'impact': 'high',
                })
        priority_order = {'P0': 0, 'P1': 1, 'P2': 2}
        recommendations.sort(key=lambda item: priority_order.get(item['priority'], 3))
        return recommendations

    def detect_format(self, content: str) -> str:
        """Detect LCOV, Istanbul JSON, Cobertura XML, or JaCoCo XML input."""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Coverage report must be non-empty text")
        content_stripped = content.strip()
        if content_stripped.startswith('TN:') or 'SF:' in content_stripped[:100]:
            return CoverageFormat.LCOV
        if content_stripped.startswith('{') or content_stripped.startswith('['):
            try:
                json.loads(content_stripped)
                return CoverageFormat.JSON
            except json.JSONDecodeError as exc:
                raise ValueError(f"Content looks like JSON but is invalid: {exc}") from exc
        xml_start = content_stripped
        if xml_start.startswith('<?xml'):
            declaration_end = xml_start.find('?>')
            xml_start = xml_start[declaration_end + 2:].lstrip() if declaration_end >= 0 else xml_start
        if xml_start.startswith('<coverage') or xml_start.startswith('<report'):
            return CoverageFormat.XML
        raise ValueError("Unable to detect coverage report format")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Parse coverage reports and emit summary, gaps, and recommendations."
    )
    parser.add_argument("--report", required=True, help="Coverage report path.")
    parser.add_argument(
        "--format",
        choices=[CoverageFormat.LCOV, CoverageFormat.JSON, CoverageFormat.XML, "auto"],
        default="auto",
        help="Coverage report format (default: auto).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="Minimum acceptable line/branch coverage percentage.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    analyzer = CoverageAnalyzer()

    try:
        content = read_text(path=args.report)
        format_type = analyzer.detect_format(content) if args.format == "auto" else args.format
        analyzer.parse_coverage_report(content, format_type)
        summary = analyzer.calculate_summary()
        gaps = analyzer.identify_gaps(args.threshold)
        recommendations = analyzer.generate_recommendations()
        emit_json(
            {
                "format": format_type,
                "threshold": args.threshold,
                "summary": summary,
                "gaps": gaps,
                "recommendations": recommendations,
            }
        )
        return 0
    except (SkillCliError, ValueError, KeyError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
