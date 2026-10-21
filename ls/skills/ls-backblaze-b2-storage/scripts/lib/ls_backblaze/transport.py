"""Bounded B2 HTTPS JSON requests, with no redirects or mutation replay."""
from __future__ import annotations

import base64
import json
import ssl
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import validate_endpoint
from .reporting import ToolError

MAX_RESPONSE = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class NativeClient:
    def __init__(self, values: dict[str, Any], budget: int = 300):
        if type(budget) is not int or not 1 <= budget <= 300:
            raise ToolError("configuration_invalid", "request_budget_seconds must be 1..300")
        self.values, self.deadline = values, time.monotonic() + budget
        context = ssl.create_default_context(cafile=values.get("ca_bundle"))
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.authorization: dict[str, Any] | None = None
        self.sends = 0

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ToolError("request_budget_expired", "request budget expired", "service", True)
        return remaining

    def _once(self, url: str, payload: Any, *, basic: bool, safe: bool) -> Any:
        body = None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if basic:
            credential = f"{self.values['key_id']}:{self.values['application_key']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credential).decode()
        elif self.authorization:
            headers["Authorization"] = self.authorization["authorizationToken"]
        if self.sends >= 9:
            raise ToolError("request_attempt_budget_exhausted", "native invocation reached its nine-send budget", "service", False)
        self.sends += 1
        try:
            with self.opener.open(Request(url, data=body, headers=headers, method="GET" if payload is None else "POST"), timeout=min(5, self._remaining())) as response:
                if response.geturl() != url:
                    raise ToolError("redirect_rejected", "server redirected a credentialed request", "service")
                raw = bytearray()
                while True:
                    remaining = self._remaining()
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(min(60, remaining))
                    block = response.read(min(65536, MAX_RESPONSE + 1 - len(raw)))
                    if not block:
                        break
                    raw.extend(block)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("response size exceeded")
                return json.loads(raw.decode("utf-8"))
        except ToolError as exc:
            if not safe and exc.code == "request_budget_expired":
                raise ToolError("write_outcome_unknown", "request budget expired while reading a mutation response", "uncertain", False, "run the corresponding read-only get/list operation") from exc
            raise
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise ToolError("redirect_rejected", "credentialed redirects are rejected", "service") from exc
            if exc.code == 401:
                # Only documented expired/bad auth token responses permit refresh.
                try:
                    code = json.loads(exc.read(65536)).get("code")
                except (ValueError, AttributeError):
                    code = None
                raise ToolError("authorization_expired" if code in {"expired_auth_token", "bad_auth_token"} else "authorization_rejected", "B2 authorization was rejected", "service") from exc
            if not safe and exc.code >= 500:
                raise ToolError("write_outcome_unknown", "gateway/service failure did not confirm write outcome", "uncertain", False, "run the corresponding read-only get/list operation") from exc
            error = ToolError("service_error", f"B2 service returned HTTP {exc.code}", "service", safe and exc.code in {429, 500, 502, 503, 504})
            try:
                error.retry_after_seconds = min(30, max(0, int(exc.headers.get("Retry-After", 0))))
            except (TypeError, ValueError):
                error.retry_after_seconds = 0
            raise error from exc
        except (URLError, TimeoutError, ValueError, OSError) as exc:
            if safe:
                raise ToolError("transport_failure", "native read failed or returned malformed JSON", "service", True) from exc
            raise ToolError("write_outcome_unknown", "write response was not confirmed; do not replay", "uncertain", False, "run the corresponding read-only get/list operation") from exc

    def _request(self, url: str, payload: Any | None, *, basic: bool = False, safe: bool = False, refresh: bool = False) -> Any:
        refreshed = False
        for attempt in range(3 if safe else 1):
            self._remaining()
            try:
                return self._once(url, payload, basic=basic, safe=safe)
            except ToolError as exc:
                if safe and refresh and not refreshed and exc.code == "authorization_expired" and attempt < 2:
                    self.authorize()
                    # Keep the original path/query, but revalidate refreshed host.
                    url = self._api() + "/b2api/" + url.split("/b2api/", 1)[1]
                    refreshed = True
                    continue
                if not safe or not exc.retryable or attempt == 2:
                    raise
                delay = min(30, max(2**attempt, getattr(exc, "retry_after_seconds", 0)))
                if self._remaining() <= delay:
                    raise
                time.sleep(delay)
        raise AssertionError("unreachable retry state")

    def authorize(self) -> dict[str, Any]:
        data = self._request(self.values["endpoint"] + "/b2api/v4/b2_authorize_account", None, basic=True, safe=True)
        if not isinstance(data, dict) or not isinstance(data.get("authorizationToken"), str) or not data["authorizationToken"] or not isinstance(data.get("accountId"), str):
            raise ToolError("malformed_response", "authorization response was malformed", "service")
        self.authorization = data
        self._api()
        return data

    def _api(self) -> str:
        try:
            api = (self.authorization or {}).get("apiInfo", {}).get("storageApi", {}).get("apiUrl")
            return validate_endpoint(api, native=True)
        except (ToolError, AttributeError) as exc:
            raise ToolError("unsafe_endpoint", "B2 authorization supplied an unsafe API URL", "service") from exc

    def call(self, operation: str, payload: Any, *, safe: bool) -> Any:
        if not self.authorization:
            self.authorize()
        return self._request(self._api() + "/b2api/v4/" + operation, payload, safe=safe, refresh=safe)

    def get_notification_rules(self, bucket_id: str) -> Any:
        if not self.authorization:
            self.authorize()
        url = self._api() + "/b2api/v4/b2_get_bucket_notification_rules?" + urlencode({"bucketId": bucket_id})
        return self._request(url, None, safe=True, refresh=True)
