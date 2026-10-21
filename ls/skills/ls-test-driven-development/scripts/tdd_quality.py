"""Quality and refactoring heuristics for tdd_workflow."""

from __future__ import annotations

import re
from typing import List


def check_minimal_implementation(code: str) -> bool:
    lines = code.split('\n')
    non_empty_lines = [line for line in lines if line.strip() and not line.strip().startswith('#')]
    if len(non_empty_lines) > 50:
        return False

    max_depth = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped:
            indent = len(line) - len(stripped)
            max_depth = max(max_depth, indent // 4)
    return max_depth <= 3


def check_quality_improvement(original: str, refactored: str) -> bool:
    if not original.strip() or not refactored.strip():
        return False
    if original == refactored:
        return False

    original_duplicates = duplicate_line_count(original)
    refactored_duplicates = duplicate_line_count(refactored)
    original_nesting = max_nesting_depth(original)
    refactored_nesting = max_nesting_depth(refactored)
    original_lines = significant_line_count(original)
    refactored_lines = significant_line_count(refactored)
    original_avg_identifier_length = avg_identifier_length(original)
    refactored_avg_identifier_length = avg_identifier_length(refactored)

    checks = [
        refactored_duplicates < original_duplicates,
        refactored_nesting < original_nesting,
        (
            refactored_avg_identifier_length > original_avg_identifier_length
            and refactored_lines <= max(original_lines + 5, int(original_lines * 1.25))
        ),
        refactored_lines < original_lines and refactored_nesting <= original_nesting,
    ]
    return any(checks)


def significant_line_count(code: str) -> int:
    return sum(
        1 for line in code.split('\n')
        if line.strip() and not line.strip().startswith(('#', '//'))
    )


def duplicate_line_count(code: str) -> int:
    counts = {}
    for line in code.split('\n'):
        stripped = line.strip()
        if len(stripped) > 10:
            counts[stripped] = counts.get(stripped, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


def max_nesting_depth(code: str) -> int:
    max_depth = 0
    for line in code.split('\n'):
        stripped = line.lstrip()
        if stripped:
            indent = len(line) - len(stripped)
            max_depth = max(max_depth, indent // 4)
    return max_depth


def avg_identifier_length(code: str) -> float:
    identifiers = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', code)
    keywords = {'if', 'else', 'for', 'while', 'def', 'class', 'return', 'import', 'from'}
    identifiers = [i for i in identifiers if i.lower() not in keywords]
    if not identifiers:
        return 0.0
    return sum(len(i) for i in identifiers) / len(identifiers)


def suggest_refactorings(code: str) -> List[str]:
    suggestions = []
    lines = code.split('\n')
    if len(lines) > 30:
        suggestions.append('Consider breaking long function into smaller functions')

    line_counts = {}
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 10:
            line_counts[stripped] = line_counts.get(stripped, 0) + 1

    duplicates = [line for line, count in line_counts.items() if count > 2]
    if duplicates:
        suggestions.append(f'Found {len(duplicates)} duplicated code patterns - consider extraction')

    magic_numbers = re.findall(r'\b\d+\b', code)
    if len(magic_numbers) > 5:
        suggestions.append('Consider extracting magic numbers to named constants')

    if 'def ' in code or 'function' in code:
        param_matches = re.findall(r'\(([^)]+)\)', code)
        for params in param_matches:
            if params.count(',') > 3:
                suggestions.append('Consider using parameter object for functions with many parameters')
                break

    if not suggestions:
        suggestions.append('Code looks clean - no obvious refactorings needed')

    return suggestions
