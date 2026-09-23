"""IMAP adapter for mail protocol control."""

from __future__ import annotations

import base64
import email
import imaplib
import re
from typing import Any

try:
    from .mail_types import AccountConfig
    from .mail_utils import as_bool, clamp_int, sanitize_list, sanitize_text
    from .mail_protocol_support import MailControlError
except ImportError:  # pragma: no cover - direct script import compatibility
    from mail_types import AccountConfig  # type: ignore
    from mail_utils import as_bool, clamp_int, sanitize_list, sanitize_text  # type: ignore
    from mail_protocol_support import MailControlError  # type: ignore

class ImapAdapter:
    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds

    def _connect(self, account: AccountConfig, creds: dict[str, str]) -> imaplib.IMAP4:
        if not account.imap_tls:
            raise MailControlError("TLS_REQUIRED", "IMAP connections require TLS.")
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            account.imap_host, account.imap_port, timeout=self.timeout_seconds
        )
        status, _ = client.login(creds["username"], creds["password"])
        if status != "OK":
            raise MailControlError("AUTH_FAILED", "IMAP authentication failed.")
        return client

    def _fetch_message_object(
        self, client: imaplib.IMAP4, uid: str, fetch_spec: str = "(BODY.PEEK[] FLAGS)",
        *, max_message_bytes: int | None = None,
    ) -> email.message.Message:
        if max_message_bytes is not None:
            return self._fetch_bounded_message(client, uid, max_message_bytes)
        f_status, f_data = client.uid("FETCH", uid, fetch_spec)
        if f_status != "OK" or not f_data:
            raise MailControlError("IMAP_FETCH_FAILED", f"Unable to fetch uid={uid}")
        raw = b""
        for part in f_data:
            if (
                isinstance(part, tuple)
                and len(part) > 1
                and isinstance(part[1], (bytes, bytearray))
            ):
                raw += bytes(part[1])
        return email.message_from_bytes(raw)

    def _fetch_bounded_message(
        self, client: imaplib.IMAP4, uid: str, limit: int
    ) -> email.message.Message:
        """Check declared size, request a partial body, then parse exact bytes.

        This bounds requested and accepted content. imaplib receives a server's
        literal before returning it, so a server violating the partial request
        can still allocate memory within imaplib. No MIME parsing occurs first.
        """
        if type(limit) is not int or not 1 <= limit <= 8 * 1024 * 1024:
            raise MailControlError("INVALID_ARGUMENT", "Invalid message byte limit.")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", uid):
            raise MailControlError("INVALID_ARGUMENT", "A numeric message UID is required.")
        status, rows = client.uid("FETCH", uid, "(UID RFC822.SIZE)")
        if status != "OK" or not rows or len(rows) != 1 or type(rows[0]) is not bytes:
            raise MailControlError("IMAP_FETCH_FAILED", "Message size is unavailable.")
        def fields(header: bytes) -> tuple[int, int]:
            if len(header) > 4096:
                raise MailControlError("IMAP_FETCH_FAILED", "Oversized message metadata.")
            ids = re.findall(rb"\bUID ([0-9]{1,20})\b", header)
            sizes = re.findall(rb"\bRFC822\.SIZE ([0-9]{1,20})\b", header)
            if len(ids) != 1 or len(sizes) != 1 or int(ids[0]) != int(uid):
                raise MailControlError("IMAP_FETCH_FAILED", "Ambiguous message identity or size.")
            return int(ids[0]), int(sizes[0])
        _, size = fields(rows[0])
        if not 0 < size <= limit:
            raise MailControlError("MESSAGE_TOO_LARGE", "Message exceeds the selected byte limit.")
        status, rows = client.uid("FETCH", uid, f"(UID RFC822.SIZE BODY.PEEK[]<0.{limit + 1}>)")
        if status != "OK" or not rows:
            raise MailControlError("IMAP_FETCH_FAILED", "Unable to retrieve bounded message.")
        literals = [row for row in rows if isinstance(row, tuple)]
        if len(literals) != 1 or any(not isinstance(row, (tuple, bytes)) for row in rows):
            raise MailControlError("IMAP_FETCH_FAILED", "Ambiguous message body.")
        literal = literals[0]
        if len(literal) != 2 or type(literal[0]) is not bytes or type(literal[1]) is not bytes:
            raise MailControlError("IMAP_FETCH_FAILED", "Malformed message body.")
        _, raw = literal
        metadata = [row[0] if isinstance(row, tuple) else row for row in rows]
        if sum(len(part) for part in metadata) > 4096:
            raise MailControlError("IMAP_FETCH_FAILED", "Oversized message metadata.")
        header = b" ".join(metadata)
        if re.fullmatch(rb"[0-9]+ \(.*\)", header) is None:
            raise MailControlError("IMAP_FETCH_FAILED", "Malformed message metadata.")
        _, returned_size = fields(header)
        if (returned_size != size or len(raw) != size or len(raw) > limit
                or re.search(rb"BODY\[\]<0> \{[0-9]+\}", header) is None):
            raise MailControlError("IMAP_FETCH_FAILED", "Incomplete or changed message body.")
        return email.message_from_bytes(raw)

    def get_capabilities(
        self, account: AccountConfig, creds: dict[str, str]
    ) -> dict[str, Any]:
        with self._connect(account, creds) as client:
            caps = sorted(
                [
                    c.decode("utf-8", errors="replace")
                    for c in (client.capabilities or [])
                ]
            )
            return {"capabilities": caps}

    def list_mailboxes(
        self, account: AccountConfig, creds: dict[str, str]
    ) -> dict[str, Any]:
        with self._connect(account, creds) as client:
            status, data = client.list()
            if status != "OK":
                raise MailControlError("IMAP_LIST_FAILED", "Unable to list mailboxes.")
            boxes = [
                line.decode("utf-8", errors="replace") for line in (data or []) if line
            ]
            return {"mailboxes": boxes}

    def query_messages(
        self, account: AccountConfig, creds: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        mailbox = sanitize_text(payload.get("mailbox", "INBOX"), 128)
        query = sanitize_text(payload.get("query", "ALL"), 256)
        lim = clamp_int(payload.get("lim"), 25, 1, 100)
        offset = clamp_int(payload.get("offset"), 0, 0, 1_000_000)
        with self._connect(account, creds) as client:
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise MailControlError(
                    "IMAP_SELECT_FAILED", f"Cannot select mailbox: {mailbox}"
                )
            status, data = client.uid("SEARCH", None, query)
            if status != "OK":
                raise MailControlError("IMAP_SEARCH_FAILED", "Search failed.")
            uids = (data[0] or b"").decode("utf-8", errors="replace").split()
            window = uids[offset : offset + lim]
            items: list[dict[str, Any]] = []
            for uid in window:
                msg = self._fetch_message_object(
                    client, uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] FLAGS)"
                )
                items.append(
                    {
                        "id": uid,
                        "from": sanitize_text(msg.get("From", ""), 256),
                        "sub": sanitize_text(msg.get("Subject", ""), 256),
                        "dt": sanitize_text(msg.get("Date", ""), 128),
                    }
                )
            next_offset = offset + len(window)
            return {
                "items": items,
                "total": len(uids),
                "next": next_offset if next_offset < len(uids) else None,
            }

    def get_message(
        self, account: AccountConfig, creds: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        mailbox = sanitize_text(payload.get("mailbox", "INBOX"), 128)
        uid = sanitize_text(payload.get("id"), 64)
        detail = as_bool(payload.get("detail"), False)
        message_limit = payload.get("max_message_bytes")
        if message_limit is not None and (
            not detail or type(message_limit) is not int
            or not 1 <= message_limit <= 8 * 1024 * 1024
        ):
            raise MailControlError("INVALID_ARGUMENT", "A valid byte limit requires full message detail.")
        include_attachment_content = as_bool(
            payload.get("include_attachment_content"), False
        )
        max_attachment_content = clamp_int(
            payload.get("max_attachment_content_bytes"),
            1024 * 1024,
            1024,
            100 * 1024 * 1024,
        )
        if not uid:
            raise MailControlError("INVALID_ARGUMENT", "Message id is required.")
        with self._connect(account, creds) as client:
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise MailControlError(
                    "IMAP_SELECT_FAILED", f"Cannot select mailbox: {mailbox}"
                )
            fetch_spec = (
                "(BODY.PEEK[] FLAGS)"
                if detail
                else "(BODY.PEEK[HEADER] FLAGS BODYSTRUCTURE)"
            )
            if message_limit is None:
                msg = self._fetch_message_object(client, uid, fetch_spec=fetch_spec)
            else:
                msg = self._fetch_message_object(
                    client, uid, fetch_spec=fetch_spec, max_message_bytes=message_limit
                )
            result: dict[str, Any] = {
                "id": uid,
                "from": sanitize_text(msg.get("From", ""), 256),
                "to": sanitize_text(msg.get("To", ""), 256),
                "cc": sanitize_text(msg.get("Cc", ""), 256),
                "sub": sanitize_text(msg.get("Subject", ""), 256),
                "dt": sanitize_text(msg.get("Date", ""), 128),
            }
            attachments: list[dict[str, Any]] = []
            if detail:
                body = ""
                html_body = ""
                if msg.is_multipart():
                    for index, part in enumerate(msg.walk()):
                        if part.is_multipart():
                            continue
                        ctype = part.get_content_type()
                        disp = str(part.get("Content-Disposition", "")).lower()
                        filename = sanitize_text(part.get_filename() or "", 256)
                        payload_bytes = part.get_payload(decode=True) or b""
                        if filename or "attachment" in disp:
                            row = {
                                "attachment_index": index,
                                "filename": filename or f"attachment-{index}",
                                "content_type": ctype,
                                "size": len(payload_bytes),
                                "content_id": sanitize_text(
                                    str(part.get("Content-ID", "")), 128
                                ),
                                "content_disposition": disp,
                            }
                            if include_attachment_content:
                                if len(payload_bytes) > max_attachment_content:
                                    row["content_truncated"] = True
                                    row["content_bytes_base64"] = base64.b64encode(
                                        payload_bytes[:max_attachment_content]
                                    ).decode("utf-8")
                                else:
                                    row["content_bytes_base64"] = base64.b64encode(
                                        payload_bytes
                                    ).decode("utf-8")
                            attachments.append(row)
                            continue
                        if ctype == "text/plain":
                            body = payload_bytes.decode(errors="replace")
                        elif ctype == "text/html":
                            html_body = payload_bytes.decode(errors="replace")
                else:
                    raw_body = msg.get_payload(decode=True) or b""
                    body = raw_body.decode(errors="replace")
                    html_body = ""
                result["body"] = sanitize_text(body, 400000)
                if html_body:
                    result["body_html"] = sanitize_text(html_body, 800000)
            result["attachments"] = attachments
            return result

    def get_attachment(
        self, account: AccountConfig, creds: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        mailbox = sanitize_text(payload.get("mailbox", "INBOX"), 128)
        uid = sanitize_text(payload.get("id"), 64)
        attachment_index = clamp_int(payload.get("attachment_index"), -1, -1, 10_000)
        if not uid:
            raise MailControlError("INVALID_ARGUMENT", "Message id is required.")
        if attachment_index < 0:
            raise MailControlError("INVALID_ARGUMENT", "attachment_index is required.")
        chunk_size = clamp_int(payload.get("chunk_size"), 256 * 1024, 1024, 1024 * 1024)
        offset = clamp_int(payload.get("offset"), 0, 0, 1_000_000_000)
        with self._connect(account, creds) as client:
            status, _ = client.select(mailbox, readonly=True)
            if status != "OK":
                raise MailControlError(
                    "IMAP_SELECT_FAILED", f"Cannot select mailbox: {mailbox}"
                )
            msg = self._fetch_message_object(
                client, uid, fetch_spec="(BODY.PEEK[] FLAGS)"
            )
            candidates: list[email.message.Message] = []
            for part in msg.walk():
                if part.is_multipart():
                    continue
                disp = str(part.get("Content-Disposition", "")).lower()
                filename = part.get_filename()
                if filename or "attachment" in disp:
                    candidates.append(part)
            if attachment_index >= len(candidates):
                raise MailControlError(
                    "ATTACHMENT_NOT_FOUND", "attachment_index out of range."
                )
            target = candidates[attachment_index]
            content = target.get_payload(decode=True) or b""
            end = min(len(content), offset + chunk_size)
            chunk = content[offset:end]
            filename = sanitize_text(
                target.get_filename() or f"attachment-{attachment_index}", 256
            )
            return {
                "id": uid,
                "attachment_index": attachment_index,
                "filename": filename,
                "content_type": target.get_content_type(),
                "size": len(content),
                "offset": offset,
                "chunk_size": len(chunk),
                "content_bytes_base64": base64.b64encode(chunk).decode("utf-8"),
                "next_offset": end if end < len(content) else None,
                "done": end >= len(content),
            }

    def mutate(
        self, account: AccountConfig, creds: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        action = sanitize_text(payload.get("mutate_action"), 64)
        mailbox = sanitize_text(payload.get("mailbox", "INBOX"), 128)
        uids = sanitize_list(payload.get("uids", []), 64, 1000)
        with self._connect(account, creds) as client:
            status, _ = client.select(mailbox)
            if status != "OK":
                raise MailControlError(
                    "IMAP_SELECT_FAILED", f"Cannot select mailbox: {mailbox}"
                )
            uid_set = ",".join(uids)
            if action in {"set_flags", "clear_flags"}:
                flags = sanitize_text(payload.get("flags", "\\Seen"), 128)
                op = "+FLAGS" if action == "set_flags" else "-FLAGS"
                m_status, _ = client.uid("STORE", uid_set, op, flags)
                if m_status != "OK":
                    raise MailControlError("IMAP_STORE_FAILED", "Flag update failed.")
                return {"updated": len(uids), "action": action}
            if action == "copy_messages":
                target = sanitize_text(payload.get("target_mailbox"), 128)
                m_status, _ = client.uid("COPY", uid_set, target)
                if m_status != "OK":
                    raise MailControlError("IMAP_COPY_FAILED", "Copy failed.")
                return {"copied": len(uids), "target": target}
            if action == "move_messages":
                target = sanitize_text(payload.get("target_mailbox"), 128)
                supports_move = b"MOVE" in (client.capabilities or ())
                if supports_move:
                    m_status, _ = client.uid("MOVE", uid_set, target)
                    if m_status != "OK":
                        raise MailControlError("IMAP_MOVE_FAILED", "MOVE failed.")
                else:
                    c_status, _ = client.uid("COPY", uid_set, target)
                    if c_status != "OK":
                        raise MailControlError(
                            "IMAP_MOVE_FAILED", "MOVE fallback COPY failed."
                        )
                    s_status, _ = client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                    if s_status != "OK":
                        raise MailControlError(
                            "IMAP_MOVE_FAILED", "MOVE fallback STORE failed."
                        )
                    if b"UIDPLUS" not in (client.capabilities or ()):
                        raise MailControlError(
                            "IMAP_MOVE_INCOMPLETE",
                            "MOVE fallback marked selected messages deleted; explicit expunge is required.",
                        )
                    e_status, _ = client.uid("EXPUNGE", uid_set)
                    if e_status != "OK":
                        raise MailControlError(
                            "IMAP_MOVE_INCOMPLETE",
                            "MOVE fallback could not expunge only the selected messages.",
                        )
                return {"moved": len(uids), "target": target}
            if action == "delete_messages":
                s_status, _ = client.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
                if s_status != "OK":
                    raise MailControlError("IMAP_DELETE_FAILED", "Delete mark failed.")
                return {"deleted_marked": len(uids)}
            if action == "expunge_mailbox":
                e_status, _ = client.expunge()
                if e_status != "OK":
                    raise MailControlError("IMAP_EXPUNGE_FAILED", "Expunge failed.")
                return {"expunged": True}
            if action == "create_mailbox":
                target = sanitize_text(payload.get("target_mailbox"), 128)
                c_status, _ = client.create(target)
                if c_status != "OK":
                    raise MailControlError(
                        "IMAP_CREATE_FAILED", "Create mailbox failed."
                    )
                return {"created": target}
            if action == "rename_mailbox":
                src = sanitize_text(payload.get("source_mailbox"), 128)
                dst = sanitize_text(payload.get("target_mailbox"), 128)
                r_status, _ = client.rename(src, dst)
                if r_status != "OK":
                    raise MailControlError(
                        "IMAP_RENAME_FAILED", "Rename mailbox failed."
                    )
                return {"renamed": {"from": src, "to": dst}}
            if action == "delete_mailbox":
                target = sanitize_text(payload.get("target_mailbox"), 128)
                d_status, _ = client.delete(target)
                if d_status != "OK":
                    raise MailControlError(
                        "IMAP_DELETE_MAILBOX_FAILED", "Delete mailbox failed."
                    )
                return {"deleted_mailbox": target}
        raise MailControlError(
            "INVALID_ARGUMENT", f"Unsupported mutate action: {action}"
        )
