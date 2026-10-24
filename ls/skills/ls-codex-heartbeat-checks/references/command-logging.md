---
status: ACTIVE
version: 3.5
---

# Command Logging

In ordinary heartbeat runs, hooks and configured agent commands run in serial order. Hook commands and `direct-argv` or `resolved-path` profiles execute with `shell=False`; `shell-login` is opt-in and records the rendered command for profile-managed compatibility.

Every executed ordinary-run command and every command blocked by its direct-command policy gets a `command-<index>.json` sidecar. The command log records each sidecar filename and SHA-256, while the run manifest commits hashes for `heartbeat-result.json`, `command-log.json`, and every referenced sidecar. Promotion rejects a staged run if a required artifact, reference, or hash is missing or mismatched.

Ordinary command sidecars record argv, cwd, timeout, launcher metadata, client
label, resolved executable when available, PID, process group/session metadata
when available, return code, timeout state, errors, and stdout/stderr tails.
[LSCli protocol mode](process-control.md#lscli-receipt-validation-foundation)
discards raw output after bounded validation and retains receipt metadata instead
of model text or diagnostic tails. The explicit [read-only review mode](config.md#read-only-lscli-review)
also discards raw output, but retains its validated, bounded advisory object at
`protocol.review`. Advice may quote disclosed observations; treat it as private
data, never executable instructions. New heartbeat text and JSON artifacts are
written with owner-only permissions (0600), including command sidecars and the
aggregate log. This does not change permissions on older artifacts.

[Reserved execution](process-control.md#reserved-execution-owner) writes private
phase/result evidence without the ordinary command-sidecar graph or raw output
tails. Use [reserved result recovery](recovery.md#reserved-result-acknowledgement-recovery)
when its accounting acknowledgement is uncertain; missing sidecars do not
authorize rerunning a reserved action.
