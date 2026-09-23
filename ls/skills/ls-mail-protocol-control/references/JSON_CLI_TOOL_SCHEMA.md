# JSON CLI tool schema

Run `python3 scripts/mail_json_cli.py --tool <tool-name> --args-json '<json-object>'`. The bridge accepts exactly one request, writes one JSON response, and exits; it does not implement an MCP transport.

## Atomic tools

| Tool | Required args | Purpose |
|---|---|---|
| `mail_accounts_list` | none | List configured delegated accounts |
| `mail_capabilities_get` | `acct` | Return SMTP and IMAP capabilities |
| `mail_query` | `acct` | Query mailbox with pagination |
| `mail_get` | `acct`, `id` | Fetch message headers or full body |
| `mail_get_attachment` | `acct`, `id`, `attachment_index` | Fetch one attachment payload chunk |
| `mail_mutate` | `acct`, `mutate_action` | Run IMAP mutation |
| `mail_send` | `acct`, `from`, `to`, `subject`, `body` | Send SMTP message |
| `mail_encrypt` | `acct`, `encryption_mode` | Encrypt full envelope payload |
| `mail_decrypt` | `acct`, `encrypted`, `encryption_mode` | Decrypt full envelope payload |
| `mail_send_encrypted` | `acct`, `from`, `to`, `subject`, `encryption_mode` | Encrypt then send secure message |
| `mail_get_decrypted` | `acct`, `id`, `encryption_mode` | Fetch encrypted message and decrypt envelope |
| `mail_sync` | `acct` | Return incremental sync cursor |
| `mail_policy_preview` | `acct`, `action` | Explain policy result |

## Composite tools

| Tool | Required args | Purpose |
|---|---|---|
| `mail_triage_batch` | `acct` | Query and apply batch mailbox actions |
| `mail_reply_flow` | `acct`, `id`, `from`, `body` | Fetch context and send reply |

## Response shape

```json
{
  "ok": true,
  "code": "OK",
  "op_id": "optional-id",
  "next": "optional-cursor-or-offset",
  "next_actions": ["mail_get", "mail_mutate"]
}
```

## Error shape

```json
{
  "ok": false,
  "code": "ACTION_BLOCKED",
  "message": "policy_deny"
}
```

## Attachment transfer conventions

- `mail_get` returns attachment metadata only by default.
- `mail_get_attachment` returns a single chunk:
  - `offset`
  - `chunk_size`
  - `next_offset`
  - `done`
  - `content_bytes_base64`

### Bounded full-message reads

`mail_get` accepts optional `max_message_bytes` with `detail: true`. The value
must be an integer from 1 through 8,388,608. The adapter checks `RFC822.SIZE`,
requests a bounded partial body, and rejects oversized, incomplete, changed, or
ambiguous responses before MIME parsing. It returns `MESSAGE_TOO_LARGE` for an
oversized advertised message and `IMAP_FETCH_FAILED` for an invalid response.
Omitting the option preserves existing retrieval behavior.

This limits requested and accepted message bytes. Python's IMAP client receives
server literals before this adapter validates them, so it is not a strict memory
limit against a server that violates the partial-body request. Consumers must
also check decoded attachment size and `content_truncated`; a bounded message
read does not establish ciphertext authenticity.

## Encryption modes

- `psk`
- `password`
- `openpgp`
