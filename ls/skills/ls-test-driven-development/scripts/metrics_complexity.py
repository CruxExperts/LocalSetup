"""Complexity scoring helpers for metrics_calculator."""

from __future__ import annotations

import re


def cyclomatic_complexity(code: str) -> int:
    decision_points = 0
    for keyword in ['if', 'for', 'while', 'case', 'catch', 'except']:
        decision_points += len(re.findall(r'\b' + keyword + r'\b', code))
    decision_points += len(re.findall(r'\&\&|\|\|', code))
    return decision_points + 1


def cognitive_complexity(code: str) -> int:
    lines = code.split('\n')
    cognitive_score = 0
    nesting_level = 0

    for line in lines:
        stripped = line.strip()
        if any(keyword in stripped for keyword in ['if ', 'for ', 'while ', 'def ', 'function ', 'class ']):
            cognitive_score += (1 + nesting_level)
            if stripped.endswith(':') or stripped.endswith('{'):
                nesting_level += 1
        if stripped.startswith('}') or (stripped and not stripped.startswith(' ') and nesting_level > 0):
            nesting_level = max(0, nesting_level - 1)
        if '&&' in stripped or '||' in stripped:
            cognitive_score += 1

    return cognitive_score


def testability_score(code: str, cyclomatic: int) -> float:
    score = 100.0

    if cyclomatic > 10:
        score -= (cyclomatic - 10) * 5
    elif cyclomatic > 5:
        score -= (cyclomatic - 5) * 2

    imports = len(re.findall(r'import |require\(|from .* import', code))
    if imports > 10:
        score -= (imports - 10) * 2

    functions = len(re.findall(r'def |function ', code))
    lines = len(code.split('\n'))
    if functions > 0:
        avg_function_size = lines / functions
        if avg_function_size < 20:
            score += 10
        elif avg_function_size > 50:
            score -= 10

    return max(0.0, min(100.0, score))


def complexity_assessment(cyclomatic: int, cognitive: int) -> str:
    if cyclomatic <= 5 and cognitive <= 10:
        return "Low complexity - easy to test"
    if cyclomatic <= 10 and cognitive <= 20:
        return "Medium complexity - moderately testable"
    if cyclomatic <= 15 and cognitive <= 30:
        return "High complexity - challenging to test"
    return "Very high complexity - consider refactoring"
