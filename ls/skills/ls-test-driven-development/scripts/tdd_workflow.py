"""
TDD workflow guidance module.

Provides step-by-step guidance through red-green-refactor cycles with validation.
"""

from typing import Dict, List, Any, Optional
import argparse
from enum import Enum

from cli_support import SkillCliError, emit_json, fail, read_json, read_text
from tdd_quality import (
    avg_identifier_length,
    check_minimal_implementation,
    check_quality_improvement,
    duplicate_line_count,
    max_nesting_depth,
    significant_line_count,
    suggest_refactorings,
)


class TDDPhase(Enum):
    """TDD cycle phases."""
    RED = "red"  # Write failing test
    GREEN = "green"  # Make test pass
    REFACTOR = "refactor"  # Improve code


class WorkflowState(Enum):
    """Current state of TDD workflow."""
    INITIAL = "initial"
    TEST_WRITTEN = "test_written"
    TEST_FAILING = "test_failing"
    TEST_PASSING = "test_passing"
    CODE_REFACTORED = "code_refactored"


class TDDWorkflow:
    """Guide users through TDD red-green-refactor workflow."""

    def __init__(self):
        """Initialize TDD workflow guide."""
        self.current_phase = TDDPhase.RED
        self.state = WorkflowState.INITIAL
        self.history = []

    def start_cycle(self, requirement: str) -> Dict[str, Any]:
        """Start a new cycle from a non-empty requirement."""
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("Requirement must be non-empty text")
        self.current_phase = TDDPhase.RED
        self.state = WorkflowState.INITIAL
        return {
            'phase': 'RED',
            'instruction': 'Write a failing test for the requirement',
            'requirement': requirement.strip(),
            'checklist': [
                'Write test that describes desired behavior',
                'Run it and capture an assertion or expectation failure',
                'Verify collection, syntax, imports, and infrastructure succeeded',
                'Record a non-empty failure message from the intended assertion',
            ],
            'tips': [
                'Focus on behavior, not implementation',
                'Start with simplest test case',
                'Test should be specific and focused',
            ],
        }

    def validate_red_phase(
        self,
        test_code: str,
        test_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Require a real assertion failure before advancing from RED."""
        if not isinstance(test_code, str):
            raise ValueError("Test code must be text")
        validations = []
        code_present = len(test_code.strip()) >= 10
        validations.append({
            'valid': code_present,
            'message': 'Test code provided' if code_present else 'No test code provided',
        })
        has_assertion = any(
            keyword in test_code.lower() for keyword in ['assert', 'expect', 'should']
        )
        validations.append({
            'valid': has_assertion,
            'message': 'Contains assertions' if has_assertion else 'Missing assertions',
        })

        if test_result is None:
            evidence_valid = False
            evidence_message = 'Missing structured failing-test evidence'
        else:
            status = self._validated_result_status(test_result)
            failure_kind = test_result.get('failure_kind')
            failure_message = test_result.get('failure_message')
            assertion_failure = failure_kind in {'assertion', 'expectation'}
            message_present = isinstance(failure_message, str) and bool(failure_message.strip())
            evidence_valid = status == 'failed' and assertion_failure and message_present
            if status != 'failed':
                evidence_message = 'RED evidence must report status failed'
            elif not assertion_failure:
                evidence_message = (
                    'RED failure_kind must be assertion or expectation; '
                    'collection, syntax, import, and infrastructure failures do not qualify'
                )
            elif not message_present:
                evidence_message = 'RED evidence requires a non-empty failure_message'
            else:
                evidence_message = 'Test fails at the intended assertion'
        validations.append({'valid': evidence_valid, 'message': evidence_message})

        if all(validation['valid'] for validation in validations):
            self.state = WorkflowState.TEST_FAILING
            self.current_phase = TDDPhase.GREEN
            return {
                'phase_complete': True,
                'next_phase': 'GREEN',
                'validations': validations,
                'instruction': 'Write minimal code to make the test pass',
            }
        return {
            'phase_complete': False,
            'current_phase': 'RED',
            'validations': validations,
            'instruction': 'Address validation issues before proceeding',
        }

    def validate_green_phase(
        self,
        implementation_code: str,
        test_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Validate GREEN phase completion.

        Args:
            implementation_code: The implementation code
            test_result: Test execution result

        Returns:
            Validation result and next steps
        """
        if not isinstance(implementation_code, str):
            raise ValueError("Implementation code must be text")
        validations = []

        # Check implementation exists
        if not implementation_code or len(implementation_code.strip()) < 5:
            validations.append({
                'valid': False,
                'message': 'No implementation code provided'
            })
        else:
            validations.append({
                'valid': True,
                'message': 'Implementation code provided'
            })

        # Check test now passes
        status = self._validated_result_status(test_result)
        test_passed = status == 'passed'
        validations.append({
            'valid': test_passed,
            'message': 'Test passes' if test_passed else 'Test still failing',
        })

        # Check for minimal implementation (heuristic)
        is_minimal = self._check_minimal_implementation(implementation_code)
        validations.append({
            'valid': is_minimal,
            'message': 'Implementation appears minimal' if is_minimal
                      else 'Implementation may be over-engineered'
        })

        all_valid = all(v['valid'] for v in validations)

        if all_valid:
            self.state = WorkflowState.TEST_PASSING
            self.current_phase = TDDPhase.REFACTOR
            return {
                'phase_complete': True,
                'next_phase': 'REFACTOR',
                'validations': validations,
                'instruction': 'Refactor code while keeping tests green',
                'refactoring_suggestions': self._suggest_refactorings(implementation_code)
            }
        else:
            return {
                'phase_complete': False,
                'current_phase': 'GREEN',
                'validations': validations,
                'instruction': 'Make the test pass before refactoring'
            }

    def validate_refactor_phase(
        self,
        original_code: str,
        refactored_code: str,
        test_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Validate REFACTOR phase completion.

        Args:
            original_code: Original implementation
            refactored_code: Refactored implementation
            test_result: Test execution result after refactoring

        Returns:
            Validation result and cycle completion status
        """
        if not isinstance(original_code, str) or not isinstance(refactored_code, str):
            raise ValueError("Original and refactored code must be text")
        if not original_code.strip() or not refactored_code.strip():
            raise ValueError("Original and refactored code must be non-empty")
        validations = []
        status = self._validated_result_status(test_result)
        test_passed = status == 'passed'
        validations.append({
            'valid': test_passed,
            'message': 'Tests still pass after refactoring' if test_passed
                       else 'Tests broken by refactoring',
        })
        code_changed = original_code != refactored_code
        validations.append({
            'valid': True,
            'message': 'Code was refactored' if code_changed
                       else 'No refactoring applied (optional)',
        })
        if code_changed:
            quality_improved = self._check_quality_improvement(original_code, refactored_code)
            validations.append({
                'valid': quality_improved,
                'message': 'Code quality improved' if quality_improved
                           else 'Changed code does not improve measured quality',
            })
        all_valid = all(validation['valid'] for validation in validations)

        if all_valid:
            self.state = WorkflowState.CODE_REFACTORED
            self.history.append({
                'cycle_complete': True,
                'final_state': self.state
            })
            return {
                'phase_complete': True,
                'cycle_complete': True,
                'validations': validations,
                'message': 'TDD cycle complete! Ready for next requirement.',
                'next_steps': [
                    'Commit your changes',
                    'Start next TDD cycle with new requirement',
                    'Or add more test cases for current feature'
                ]
            }
        else:
            return {
                'phase_complete': False,
                'current_phase': 'REFACTOR',
                'validations': validations,
                'instruction': 'Ensure tests still pass after refactoring'
            }

    def _validated_result_status(self, test_result: Dict[str, Any]) -> str:
        if not isinstance(test_result, dict):
            raise ValueError("Test result must be an object")
        status = test_result.get('status')
        if status not in {'passed', 'failed'}:
            raise ValueError("Test result status must be passed or failed")
        return status

    def _check_minimal_implementation(self, code: str) -> bool:
        return check_minimal_implementation(code)

    def _check_quality_improvement(self, original: str, refactored: str) -> bool:
        return check_quality_improvement(original, refactored)

    def _significant_line_count(self, code: str) -> int:
        return significant_line_count(code)

    def _duplicate_line_count(self, code: str) -> int:
        return duplicate_line_count(code)

    def _max_nesting_depth(self, code: str) -> int:
        return max_nesting_depth(code)

    def _avg_identifier_length(self, code: str) -> float:
        return avg_identifier_length(code)

    def _suggest_refactorings(self, code: str) -> List[str]:
        return suggest_refactorings(code)

    def generate_workflow_summary(self) -> str:
        """Generate summary of TDD workflow progress."""
        summary = [
            "# TDD Workflow Summary\n",
            f"Current Phase: {self.current_phase.value.upper()}",
            f"Current State: {self.state.value.replace('_', ' ').title()}",
            f"Completed Cycles: {len(self.history)}\n"
        ]

        summary.append("## TDD Cycle Steps:\n")
        summary.append("1. **RED**: Write a failing test")
        summary.append("   - Test describes desired behavior")
        summary.append("   - Test fails (no implementation)\n")

        summary.append("2. **GREEN**: Make the test pass")
        summary.append("   - Write minimal code to pass test")
        summary.append("   - All tests should pass\n")

        summary.append("3. **REFACTOR**: Improve the code")
        summary.append("   - Clean up implementation")
        summary.append("   - Tests still pass")
        summary.append("   - Code is more maintainable\n")

        return "\n".join(summary)

    def get_phase_guidance(self, phase: Optional[TDDPhase] = None) -> Dict[str, Any]:
        """
        Get detailed guidance for a specific phase.

        Args:
            phase: TDD phase (uses current if not specified)

        Returns:
            Detailed guidance dictionary
        """
        target_phase = phase or self.current_phase

        if target_phase == TDDPhase.RED:
            return {
                'phase': 'RED',
                'goal': 'Write a failing test',
                'steps': [
                    '1. Read and understand the requirement',
                    '2. Think about expected behavior',
                    '3. Write test that verifies this behavior',
                    '4. Run test and ensure it fails',
                    '5. Verify failure reason is correct (not syntax error)'
                ],
                'common_mistakes': [
                    'Test passes immediately (no real assertion)',
                    'Test fails for wrong reason (syntax error)',
                    'Test is too broad or tests multiple things'
                ],
                'tips': [
                    'Start with simplest test case',
                    'One assertion per test (focused)',
                    'Test should read like specification'
                ]
            }

        elif target_phase == TDDPhase.GREEN:
            return {
                'phase': 'GREEN',
                'goal': 'Make the test pass with minimal code',
                'steps': [
                    '1. Write simplest code that makes test pass',
                    '2. Run test and verify it passes',
                    '3. Run all tests to ensure no regression',
                    '4. Resist urge to add extra features'
                ],
                'common_mistakes': [
                    'Over-engineering solution',
                    'Adding features not covered by tests',
                    'Breaking existing tests'
                ],
                'tips': [
                    'Fake it till you make it (hardcode if needed)',
                    'Triangulate with more tests if needed',
                    'Keep implementation simple'
                ]
            }

        elif target_phase == TDDPhase.REFACTOR:
            return {
                'phase': 'REFACTOR',
                'goal': 'Improve code quality while keeping tests green',
                'steps': [
                    '1. Identify code smells or duplication',
                    '2. Apply one refactoring at a time',
                    '3. Run tests after each change',
                    '4. Commit when satisfied with quality'
                ],
                'common_mistakes': [
                    'Changing behavior (breaking tests)',
                    'Refactoring too much at once',
                    'Skipping this phase'
                ],
                'tips': [
                    'Extract methods for better naming',
                    'Remove duplication',
                    'Improve variable names',
                    'Tests are safety net - use them!'
                ]
            }

        return {}


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Validate or summarize a TDD red-green-refactor workflow phase."
    )
    parser.add_argument(
        "--phase",
        choices=[phase.value for phase in TDDPhase],
        help="Phase to validate or describe.",
    )
    parser.add_argument("--requirement", help="Requirement for starting a TDD cycle.")
    parser.add_argument("--test-code-file", help="Path to test code.")
    parser.add_argument("--test-code", help="Inline test code.")
    parser.add_argument("--implementation-file", help="Path to implementation code.")
    parser.add_argument("--implementation-code", help="Inline implementation code.")
    parser.add_argument("--original-file", help="Path to original implementation.")
    parser.add_argument("--original-code", help="Inline original implementation.")
    parser.add_argument("--refactored-file", help="Path to refactored implementation.")
    parser.add_argument("--refactored-code", help="Inline refactored implementation.")
    parser.add_argument("--test-result-json", help="Inline test result JSON.")
    parser.add_argument("--test-result-file", help="Path to test result JSON.")
    parser.add_argument("--summary", action="store_true", help="Print workflow summary.")
    parser.add_argument("--guidance", action="store_true", help="Print phase guidance.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    workflow = TDDWorkflow()

    try:
        if args.summary:
            print(workflow.generate_workflow_summary())
            return 0

        phase = TDDPhase(args.phase) if args.phase else workflow.current_phase
        if args.guidance or not args.phase:
            emit_json(workflow.get_phase_guidance(phase))
            return 0

        if phase == TDDPhase.RED:
            if args.requirement and not (args.test_code_file or args.test_code):
                emit_json(workflow.start_cycle(args.requirement))
                return 0
            test_code = read_text(path=args.test_code_file, inline=args.test_code)
            test_result = read_json(args.test_result_file, args.test_result_json) if (
                args.test_result_file or args.test_result_json
            ) else None
            emit_json(workflow.validate_red_phase(test_code, test_result))
        elif phase == TDDPhase.GREEN:
            implementation = read_text(
                path=args.implementation_file, inline=args.implementation_code
            )
            test_result = read_json(args.test_result_file, args.test_result_json)
            emit_json(workflow.validate_green_phase(implementation, test_result))
        elif phase == TDDPhase.REFACTOR:
            original = read_text(path=args.original_file, inline=args.original_code)
            refactored = read_text(path=args.refactored_file, inline=args.refactored_code)
            test_result = read_json(args.test_result_file, args.test_result_json)
            emit_json(workflow.validate_refactor_phase(original, refactored, test_result))
        return 0
    except (SkillCliError, ValueError, KeyError, TypeError) as exc:
        return fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
