from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class GitHubAPI:
    repository: str
    token: str = ""
    api_url: str = "https://api.github.com"
    max_records: int = 10_000
    request_budget: int = 10_000

    @classmethod
    def from_env(cls) -> "GitHubAPI":
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        return cls(repository=repo, token=token, api_url=api_url)

    def enabled(self) -> bool:
        return bool(self.repository and self.token)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.enabled():
            raise RuntimeError("GitHub repository/token are not configured")
        if self.request_budget <= 0:
            raise RuntimeError("GitHub issue request budget exhausted; no further requests dispatched")
        self.request_budget -= 1
        headers = kwargs.pop("headers", {})
        headers.update({"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
        response = requests.request(method, f"{self.api_url.rstrip('/')}/{path.lstrip('/')}",
                                    headers=headers, timeout=30, allow_redirects=False, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError("GitHub issue redirect refused; reconcile the configured destination before retrying")
        response.raise_for_status()
        return response.json() if response.content else None

    def _list_pages(self, path: str, **params: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in range(1, 101):
            rows = self._request("GET", path, params={**params, "per_page": 100, "page": page})
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise RuntimeError("GitHub issue observations were not an object list")
            if len(records) + len(rows) > self.max_records:
                raise RuntimeError("GitHub issue observation inventory exceeded its bound")
            records.extend(rows)
            if len(rows) < 100:
                return records
        raise RuntimeError("GitHub issue observation pagination exceeded its bound")

    def list_open_issues(self, labels: str = "qc-patrol") -> list[dict[str, Any]]:
        return self._list_pages(f"/repos/{self.repository}/issues", state="open", labels=labels)

    def list_issues(self, labels: str, *, state: str = "open") -> list[dict[str, Any]]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("issue state must be open, closed, or all")
        rows = self._list_pages(f"/repos/{self.repository}/issues", state=state, labels=labels)
        return [row for row in rows if "pull_request" not in row]

    def list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self._list_pages(f"/repos/{self.repository}/issues/{number}/comments")

    def get_repository(self) -> dict[str, Any]:
        result = self._request("GET", f"/repos/{self.repository}")
        if not isinstance(result, dict) or type(result.get("id")) is not int:
            raise RuntimeError("GitHub repository identity was not verified")
        return result

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return dict(self._request("POST", f"/repos/{self.repository}/issues", json={"title": title, "body": body, "labels": labels}))

    def comment_issue(self, number: int, body: str) -> dict[str, Any]:
        return dict(self._request("POST", f"/repos/{self.repository}/issues/{number}/comments", json={"body": body}))
