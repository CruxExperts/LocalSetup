"""Focused behavior tests for the Arbiter filesystem CLI."""

from __future__ import annotations

import argparse
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import arbiter_cli


class ArbiterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(agent="test-agent", session="test-session")
        self.decision = {
            "id": "database",
            "title": "Database",
            "options": [
                {"key": "postgresql", "label": "PostgreSQL"},
                {"key": "sqlite", "label": "SQLite"},
            ],
        }
        self.payload = {"title": "Choose a database", "decisions": [self.decision]}

    def test_push_initializes_decisions_as_pending_without_answers(self) -> None:
        plan = arbiter_cli._normalize_plan(self.payload, self.args)

        self.assertEqual(plan["status"], "pending")
        self.assertEqual(plan["answered"], 0)
        self.assertEqual(plan["remaining"], 1)
        self.assertEqual(plan["decisions"][0]["status"], "pending")
        self.assertIsNone(plan["decisions"][0]["answer"])
        self.assertIsNone(plan["decisions"][0]["answered_at"])

    def test_push_rejects_injected_plan_completion_state(self) -> None:
        for field in arbiter_cli.PLAN_COMPLETION_FIELDS:
            with self.subTest(field=field):
                payload = dict(self.payload, **{field: "injected"})
                with self.assertRaisesRegex(arbiter_cli.InputError, "completion fields are not allowed"):
                    arbiter_cli._normalize_plan(payload, self.args)

    def test_push_rejects_injected_decision_completion_state(self) -> None:
        for field in arbiter_cli.DECISION_COMPLETION_FIELDS:
            with self.subTest(field=field):
                decision = dict(self.decision, **{field: "injected"})
                payload = {"title": self.payload["title"], "decisions": [decision]}
                with self.assertRaisesRegex(arbiter_cli.InputError, "completion fields are not allowed"):
                    arbiter_cli._normalize_plan(payload, self.args)

    def test_completion_ignores_declared_status_and_requires_valid_option_answer(self) -> None:
        metadata = {
            "planId": "plan-1",
            "status": "completed",
            "answered": 1,
            "remaining": 0,
            "answers": {"database": "postgresql"},
            "decisions": [dict(self.decision, status="answered", answer="mysql")],
        }

        status = arbiter_cli._status_from_metadata(metadata)

        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["answered"], 0)
        self.assertEqual(status["remaining"], 1)
        self.assertEqual(status["decisions"]["database"], {"status": "pending", "answer": None})
        self.assertEqual(arbiter_cli._answers_from_metadata(metadata), {})

    def test_completion_accepts_only_nonempty_string_answers(self) -> None:
        invalid_answers = (None, "", "   ", 1, True)
        for answer in invalid_answers:
            with self.subTest(answer=answer):
                metadata = {"decisions": [dict(self.decision, allowCustom=True, answer=answer)]}
                self.assertEqual(arbiter_cli._status_from_metadata(metadata)["status"], "pending")

        option_metadata = {"decisions": [dict(self.decision, answer="postgresql")]}
        self.assertEqual(arbiter_cli._status_from_metadata(option_metadata)["status"], "completed")
        self.assertEqual(arbiter_cli._answers_from_metadata(option_metadata), {"database": "postgresql"})

        custom_metadata = {"decisions": [dict(self.decision, allowCustom=True, answer="  MariaDB  ")]}
        self.assertEqual(arbiter_cli._status_from_metadata(custom_metadata)["status"], "completed")
        self.assertEqual(arbiter_cli._answers_from_metadata(custom_metadata), {"database": "MariaDB"})

    def test_completion_requires_unique_nonempty_decision_ids(self) -> None:
        missing_id = dict(self.decision, answer="postgresql")
        missing_id.pop("id")
        duplicate_ids = {
            "decisions": [
                dict(self.decision, answer="postgresql"),
                dict(self.decision, answer="sqlite"),
            ]
        }

        self.assertEqual(arbiter_cli._status_from_metadata({"decisions": [missing_id]})["status"], "pending")
        self.assertEqual(arbiter_cli._status_from_metadata(duplicate_ids)["status"], "pending")

    def test_find_plan_uses_frontmatter_decision_answers(self) -> None:
        metadata = {
            "id": "plan-1",
            "planId": "plan-1",
            "status": "completed",
            "answered": 1,
            "remaining": 0,
            "decisions": [dict(self.decision, status="answered", answer="sqlite")],
        }
        body = "answer: null\n"

        with (
            mock.patch.object(arbiter_cli, "_iter_plan_files", return_value=[Path("plan.md")]),
            mock.patch.object(arbiter_cli, "_load_plan", return_value=(metadata, body)),
        ):
            _path, loaded, content = arbiter_cli._find_plan(Path("queue"), plan_id="plan-1")

        self.assertIs(loaded, metadata)
        self.assertEqual(content, body)
        self.assertEqual(arbiter_cli._status_from_metadata(loaded)["status"], "completed")
        self.assertEqual(arbiter_cli._answers_from_metadata(loaded), {"database": "sqlite"})

    def test_get_rejects_metadata_marked_complete_without_valid_answers(self) -> None:
        metadata = {
            "planId": "plan-1",
            "status": "completed",
            "decisions": [dict(self.decision, status="answered", answer=None)],
        }
        args = argparse.Namespace(queue_dir="unused", plan_id="plan-1", tag=None)
        stderr = io.StringIO()

        with (
            mock.patch.object(arbiter_cli, "_ensure_queue"),
            mock.patch.object(arbiter_cli, "_find_plan", return_value=(Path("plan.md"), metadata, "")),
            contextlib.redirect_stderr(stderr),
        ):
            result = arbiter_cli.cmd_get(args)

        self.assertEqual(result, 1)
        self.assertIn("Plan not complete", stderr.getvalue())

    def test_await_caps_sleep_to_remaining_deadline(self) -> None:
        metadata = {"planId": "plan-1", "decisions": [dict(self.decision, answer=None)]}
        args = argparse.Namespace(queue_dir="unused", plan_id="plan-1", tag=None, timeout=10, interval=30)
        stderr = io.StringIO()

        with (
            mock.patch.object(arbiter_cli, "_ensure_queue"),
            mock.patch.object(arbiter_cli, "_find_plan", return_value=(Path("plan.md"), metadata, "")),
            mock.patch.object(arbiter_cli.time, "monotonic", side_effect=[100.0, 103.0, 110.0]),
            mock.patch.object(arbiter_cli.time, "sleep") as sleep,
            contextlib.redirect_stderr(stderr),
        ):
            result = arbiter_cli.cmd_await(args)

        self.assertEqual(result, 1)
        sleep.assert_called_once_with(7.0)
        self.assertIn("Timed out waiting for plan", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
