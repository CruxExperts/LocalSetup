"""Allowlisted Admin v2 operations with protected results and bounded transport."""
from __future__ import annotations

import json
import os
import ssl
import time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import admin_schema
from .http_transport import DeadlineHTTPHandler, DeadlineHTTPSHandler, decode, read, remaining, retry_delay
from .reporting import ToolError
from .responses import project
from .secret_files import deliver_reserved, read_restore, reserve


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def _request(op, args, values):
    selected = read_restore(args["restore_file"]) if op["operationId"] == "ImportKey" else dict(args)
    query_names = {item["name"] for item in op.get("parameters", [])}
    query = {key: value for key, value in selected.items() if key in query_names}
    if op["operationId"] == "GetKeyInfo":
        query["showSecretKey"] = "false"
    body = {key: value for key, value in selected.items() if key not in query_names and key not in {"secret_output", "restore_existing", "restore_file"}}
    raw = None if op["method"] == "GET" else json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
    url = values["endpoint"] + op["path"] + (("?" + urlencode(query)) if query else "")
    return Request(url, data=raw, method=op["method"], headers={"Authorization": "Bearer " + values["token"],
                   "Accept": "application/json", **({"Content-Type": "application/json"} if raw is not None else {})})


def _send(request, opener, safe, deadline):
    for attempt in range(3 if safe else 1):
        remaining(deadline)
        try:
            with opener.open(request, timeout=min(5, remaining(deadline))) as response:
                if response.geturl() != request.full_url:
                    raise ToolError("redirect_rejected", "admin redirects are rejected", "service")
                raw = read(response, deadline)
                return decode(raw) if raw else None
        except HTTPError as exc:
            if safe and exc.code in {429, 500, 502, 503, 504} and attempt < 2 and retry_delay(attempt, exc.headers, deadline):
                continue
            if not safe and exc.code >= 500:
                raise ToolError("write_outcome_unknown", "gateway/service failure did not confirm the write", "uncertain", False,
                                "use the corresponding Get/List operation before retrying") from exc
            raise ToolError("redirect_rejected" if 300 <= exc.code < 400 else "service_rejected",
                            f"Garage Admin returned HTTP {exc.code}", "service", safe and exc.code in {429, 500, 502, 503, 504}) from exc
        except ToolError as exc:
            if not safe and exc.code == "request_budget_expired":
                raise ToolError("write_outcome_unknown", "deadline expired after write dispatch", "uncertain", False,
                                "use the corresponding Get/List operation before retrying") from exc
            raise
        except (URLError, OSError, ValueError) as exc:
            if safe and attempt < 2 and retry_delay(attempt, None, deadline):
                continue
            raise ToolError("transport_failure", "Garage Admin response was not confirmed", "service" if safe else "uncertain", safe,
                            "use the corresponding Get/List operation before retrying" if not safe else None) from exc
    raise AssertionError("unreachable request attempts")


def execute(name, args, values):
    op = admin_schema.operation(name)
    safe = op["method"] == "GET"
    deadline = time.monotonic() + values.get("request_budget_seconds", 300)
    context = ssl.create_default_context(cafile=values.get("ca_bundle"))
    opener = build_opener(NoRedirect(), DeadlineHTTPSHandler(context, deadline), DeadlineHTTPHandler(deadline))
    request = _request(op, args, values)
    reserved = reserve(args["secret_output"]) if name == "CreateKey" else None
    try:
        data = _send(request, opener, safe, deadline)
        if name in {"DeleteBucket", "DeleteKey"} and data is None:
            data = {"deleted": args["id"]}
        if name == "CreateKey":
            key_id = data.get("accessKeyId") if isinstance(data, dict) else None
            secret = data.get("secretAccessKey") if isinstance(data, dict) else None
            if not isinstance(key_id, str) or not key_id:
                raise ToolError("malformed_response", "key creation omitted its identifier", "uncertain", False, "inspect ListKeys before retrying")
            if not isinstance(secret, str) or not secret:
                return {"partial": True, "access_key_id": key_id, "reconciliation": "key exists but no secret was delivered; inspect GetKeyInfo before any new action"}
            try:
                deliver_reserved(reserved, {"accessKeyId": key_id, "secretAccessKey": secret, "name": data.get("name", "")})
            except ToolError:
                return {"partial": True, "access_key_id": key_id, "reconciliation": "key exists but secret delivery failed; preserve output and reconcile GetKeyInfo before any new action"}
        try:
            return project(data, admin_schema.result_schema(name), {"$defs": admin_schema.definitions(public_result=True)})
        except (ValueError, TypeError) as exc:
            raise ToolError("malformed_response", "admin response did not satisfy its result contract", "service" if safe else "uncertain", False,
                            "inspect the corresponding Get/List result before retrying" if not safe else None) from exc
    finally:
        if reserved is not None:
            os.close(reserved[1])
