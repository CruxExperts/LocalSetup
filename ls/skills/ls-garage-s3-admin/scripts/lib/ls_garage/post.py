"""Bounded POST form signing and streaming; encrypted forms stay protected."""
from __future__ import annotations

import hashlib
import http.client
import os
import re
import ssl
import stat
from pathlib import Path
from urllib.parse import urlsplit

from .encryption import parameters
from .http_transport import HTTPConnection, HTTPSConnection, read, remaining
from .reporting import ToolError
from .secret_files import write
from .transfers import FileRange

HEADERS = {"SSECustomerAlgorithm": "x-amz-server-side-encryption-customer-algorithm",
           "SSECustomerKey": "x-amz-server-side-encryption-customer-key",
           "SSECustomerKeyMD5": "x-amz-server-side-encryption-customer-key-MD5"}


def form(client, args, *, expires=300, size=None):
    fields = {"key": args["key"]}
    if "content_type" in args:
        fields["Content-Type"] = args["content_type"]
    fields.update({HEADERS[key]: value for key, value in parameters(args.get("encryption")).items()})
    conditions = [{key: value} for key, value in fields.items()]
    conditions.append(["content-length-range", args.get("min_content_length", 0) if size is None else size,
                       args["max_content_length"] if size is None else size])
    result = client.generate_presigned_post(args["bucket"], args["key"], Fields=fields, Conditions=conditions, ExpiresIn=expires)
    if not isinstance(result, dict) or not isinstance(result.get("url"), str) or not isinstance(result.get("fields"), dict):
        raise ToolError("malformed_response", "generated POST form was malformed", "service")
    for key, value in result["fields"].items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key) or not isinstance(value, str):
            raise ToolError("malformed_response", "generated POST form had unsafe field names/types", "service")
    return result


def presign(client, args):
    result = form(client, args, expires=args["expires_seconds"])
    if args.get("encryption"):
        # The base64 policy also embeds the customer key. Redacting a single
        # form field cannot make the remaining form safe for ordinary JSON.
        write(args["secret_output"], result)
        return {"secret_output": args["secret_output"], "sensitive": True, "expires_seconds": args["expires_seconds"]}
    return {**result, "sensitive": True, "expires_seconds": args["expires_seconds"]}


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def upload(client, args, values, deadline):
    try:
        stream = Path(args["source"]).open("rb")
    except OSError as exc:
        raise ToolError("source_missing", "POST source cannot be opened") from exc
    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 5 * 1024**3:
            raise ToolError("source_invalid", "POST source must be a regular file no larger than5GiB")
        signed = form(client, args, size=before.st_size)
        parsed, configured = urlsplit(signed["url"]), urlsplit(values["endpoint_url"])
        authority = lambda value: (value.scheme, value.hostname, value.port or (443 if value.scheme == "https" else 80))
        if authority(parsed) != authority(configured) or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ToolError("unsafe_endpoint", "POST destination differs from the explicitly configured endpoint", "policy")
        if parsed.scheme not in {"https", "http"} or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
            raise ToolError("unsafe_endpoint", "POST endpoint is not an approved HTTPS or literal-loopback endpoint", "policy")
        boundary = "garage-post-" + hashlib.sha256(os.urandom(32)).hexdigest()
        pieces = [(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                  for key, value in signed["fields"].items()]
        prefix = b"".join(pieces) + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="upload"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        suffix = f"\r\n--{boundary}--\r\n".encode()
        context = ssl.create_default_context(cafile=values.get("verify") if isinstance(values.get("verify"), str) else None)
        connection = (HTTPSConnection(parsed.hostname, parsed.port or 443, context=context, deadline=deadline)
                      if parsed.scheme == "https" else HTTPConnection(parsed.hostname, parsed.port or 80, deadline=deadline))
        dispatched = False
        try:
            remaining(deadline)
            connection.putrequest("POST", parsed.path or "/")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(prefix) + before.st_size + len(suffix)))
            dispatched = True
            connection.endheaders()
            connection.send(prefix)
            body = FileRange(stream, 0, before.st_size)
            while True:
                remaining(deadline)
                block = body.read(1024 * 1024)
                if not block:
                    break
                if connection.sock is not None:
                    connection.sock.settimeout(min(60, remaining(deadline)))
                connection.send(block)
            if body.tell() != before.st_size:
                raise OSError("POST source shortened")
            remaining(deadline)
            connection.send(suffix)
            response = connection.getresponse()
            read(response, deadline)
            if response.status not in {200, 201, 204}:
                uncertain = response.status >= 500
                raise ToolError("write_outcome_unknown" if uncertain else "service_rejected", f"POST returned HTTP {response.status}",
                                "uncertain" if uncertain else "service", False, "inspect HeadObject before retrying" if uncertain else None)
            changed = _identity(before) != _identity(os.fstat(stream.fileno()))
        except ToolError as exc:
            if dispatched and exc.code == "request_budget_expired":
                raise ToolError("write_outcome_unknown", "POST deadline expired after dispatch", "uncertain", False, "inspect HeadObject before retrying") from exc
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ToolError("write_outcome_unknown", "POST response was not confirmed; do not replay", "uncertain", False, "inspect HeadObject before retrying") from exc
        finally:
            connection.close()
    return {"etag": None, "content_length": before.st_size, "assurance": "confirmed streaming POST response and source length; no whole-file provider checksum",
            **({"partial": True, "reconciliation": "source changed during upload; inspect HeadObject"} if changed else {})}
