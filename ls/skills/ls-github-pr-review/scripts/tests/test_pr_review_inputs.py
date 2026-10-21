import importlib.util
import subprocess
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "pr_review.py"


def load_pr_review():
    spec = importlib.util.spec_from_file_location("pr_review", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def gh_result(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def test_get_open_prs_fails_on_gh_error(monkeypatch):
    pr_review = load_pr_review()
    monkeypatch.setattr(pr_review, "_gh", lambda *args, **kwargs: gh_result(1, stderr="authentication required"))

    with pytest.raises(pr_review.ReviewInputError, match="open PR list.*authentication required"):
        pr_review.get_open_prs("owner/repo")


def test_get_open_prs_fails_on_bad_json(monkeypatch):
    pr_review = load_pr_review()
    monkeypatch.setattr(pr_review, "_gh", lambda *args, **kwargs: gh_result(0, stdout="{not json"))

    with pytest.raises(pr_review.ReviewInputError, match="open PR list JSON"):
        pr_review.get_open_prs("owner/repo")


def test_get_pr_commits_fails_when_commits_shape_is_bad(monkeypatch):
    pr_review = load_pr_review()
    monkeypatch.setattr(pr_review, "_gh", lambda *args, **kwargs: gh_result(0, stdout='{"commits": {}}'))

    with pytest.raises(pr_review.ReviewInputError, match="commits must be a list"):
        pr_review.get_pr_commits("owner/repo", 42)


def test_load_state_fails_on_bad_json(tmp_path):
    pr_review = load_pr_review()
    state_path = tmp_path / "pr-reviews.json"
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(pr_review.ReviewInputError, match="Could not parse review state JSON"):
        pr_review.load_state(state_path)


def test_is_reviewed_fails_when_head_sha_unavailable(monkeypatch, tmp_path):
    pr_review = load_pr_review()
    state_path = tmp_path / "pr-reviews.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pr_review, "_gh", lambda *args, **kwargs: gh_result(0, stdout=""))

    with pytest.raises(pr_review.ReviewInputError, match="head SHA"):
        pr_review.is_reviewed("owner/repo", 42, state_path)
