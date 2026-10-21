"""One explicit retry boundary per SDK request, never per transfer workflow."""
from __future__ import annotations

import time
from typing import Any

from .reporting import ToolError


def translate(exc: Exception, *, safe: bool) -> ToolError:
    # Import lazily so help, schemas and native administration need no SDK.
    from botocore.exceptions import ClientError, ParamValidationError

    if isinstance(exc, ParamValidationError):
        return ToolError("invalid_request", "request did not satisfy the supported SDK protocol")
    if isinstance(exc, ClientError):
        metadata = exc.response.get("ResponseMetadata", {})
        status = metadata.get("HTTPStatusCode")
        # A gateway error on a write does not prove the upstream mutation failed.
        if not safe and status in {500, 502, 503, 504}:
            return uncertain()
        error = ToolError("service_rejected", f"S3 service rejected the request (HTTP {status or 'unknown'})", "service", safe and status in {429, 500, 502, 503, 504})
        try:
            error.retry_after_seconds = min(30, max(0, int(metadata.get("HTTPHeaders", {}).get("retry-after", 0))))
        except (TypeError, ValueError):
            error.retry_after_seconds = 0
        return error
    return ToolError("transport_failure", "S3 read failed", "service", True) if safe else uncertain()


def uncertain() -> ToolError:
    return ToolError("write_outcome_unknown", "write response was not confirmed; do not replay", "uncertain", False, "use HeadObject, ListParts, or the corresponding read-only get/list operation before deciding what to do")


class Requests:
    """Wrap a configured client; all calls share one caller-request deadline."""
    def __init__(self, client: Any, deadline: float):
        self.client, self.deadline = client, deadline

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.client, name)
        if name.startswith("generate_presigned_"):
            def presign(*args: Any, **kwargs: Any) -> Any:
                try:
                    return method(*args, **kwargs)
                except Exception as exc:
                    raise translate(exc, safe=True) from exc
            return presign
        safe = name.startswith(("get_", "head_", "list_"))

        def invoke(**kwargs: Any) -> Any:
            for attempt in range(3 if safe else 1):
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolError("request_budget_expired", "request budget expired before dispatch", "service", safe)
                # Botocore exposes the pool's timeout at this single-threaded
                # transport boundary. Clamp both phases before each dispatch.
                endpoint = getattr(self.client, "_endpoint", None)
                session = getattr(endpoint, "http_session", None)
                manager = getattr(session, "_manager", None)
                if manager is not None:
                    from urllib3.util import Timeout
                    manager.connection_pool_kw["timeout"] = Timeout(total=remaining, connect=min(5, remaining), read=min(60, remaining))
                    # Existing pools otherwise retain their previous timeout.
                    for key in list(manager.pools.keys()):
                        pool = manager.pools.get(key)
                        if pool is not None:
                            pool.timeout = manager.connection_pool_kw["timeout"]
                try:
                    return method(**kwargs)
                except ToolError:
                    raise
                except Exception as exc:
                    error = translate(exc, safe=safe)
                    if not safe or not error.retryable or attempt == 2:
                        raise error from exc
                    delay = min(30, max(2**attempt, getattr(error, "retry_after_seconds", 0)))
                    remaining = self.deadline - time.monotonic()
                    if remaining <= delay:
                        raise error from exc
                    time.sleep(delay)
            raise AssertionError("unreachable retry state")

        return invoke
