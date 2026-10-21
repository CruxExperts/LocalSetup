"""Deadline-bound standard-library HTTP primitives shared by admin and POST."""
from __future__ import annotations

import http.client
import json
import time
from email.utils import parsedate_to_datetime
from urllib.request import HTTPHandler, HTTPSHandler

from .reporting import ToolError

MAX_RESPONSE = 8 * 1024 * 1024


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise ToolError("request_budget_expired", "request budget expired", "service", True)
    return value


class DeadlineConnection:
    def __init__(self, *args, deadline, **kwargs):
        self.deadline = deadline
        kwargs["timeout"] = min(5, remaining(deadline))
        super().__init__(*args, **kwargs)

    def getresponse(self):
        if self.sock is not None:
            self.sock.settimeout(min(60, remaining(self.deadline)))
        return super().getresponse()


class HTTPSConnection(DeadlineConnection, http.client.HTTPSConnection):
    pass


class HTTPConnection(DeadlineConnection, http.client.HTTPConnection):
    pass


class DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, context, deadline):
        super().__init__(context=context)
        self.deadline = deadline

    def https_open(self, request):
        return self.do_open(lambda host, **kwargs: HTTPSConnection(host, deadline=self.deadline, **kwargs), request,
                            context=self._context, check_hostname=self._check_hostname)


class DeadlineHTTPHandler(HTTPHandler):
    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def http_open(self, request):
        return self.do_open(lambda host, **kwargs: HTTPConnection(host, deadline=self.deadline, **kwargs), request)


def read(response, deadline):
    output = bytearray()
    while True:
        budget = remaining(deadline)
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(min(60, budget))
        method = getattr(response, "read1", response.read)
        block = method(min(65536, MAX_RESPONSE + 1 - len(output)))
        if not block:
            return bytes(output)
        output.extend(block)
        if len(output) > MAX_RESPONSE:
            raise ValueError("response too large")


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate response field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def retry_delay(attempt, headers, deadline):
    value = headers.get("Retry-After", "0") if headers else "0"
    try:
        retry = float(value)
    except (TypeError, ValueError):
        try:
            retry = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            retry = 0
    delay = min(30, max(2**attempt, retry))
    if remaining(deadline) <= delay:
        return False
    time.sleep(delay)
    return True
