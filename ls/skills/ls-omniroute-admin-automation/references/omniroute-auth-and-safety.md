# OmniRoute Auth and Safety

## Auth model

- Runtime (`/v1/*`) credentials can differ from management (`/api/*`) credentials.
- Use dedicated management token/cookie for admin calls.
- Do not assume a runtime key can mutate admin resources.
- Run `python3 scripts/omniroute_admin.py preflight --required-access <runtime|read|write|admin>` from inside the installed package directory before automation. From a LocalSetup repo root, resolve the helper with `python3 ls/tools/localsetup.py --source-root . path package ls-omniroute-admin-automation scripts/omniroute_admin.py`. The check uses non-mutating GET endpoints and reports missing env vars, invalid credentials, or insufficient access as structured JSON.

## Secret handling

- Pull secrets from env vars only.
- Never print raw tokens.
- Do not store secrets in manifests, logs, or git history.
- If required env vars are missing, use `ls-omniroute-model-discovery` preflight with `--print-env-commands` to emit durable user-level registration commands, then relaunch shells, tmux sessions, GUI apps, and agent CLIs.

## Mutation safety

- Snapshot before mutation.
- Build plan and display summary.
- Require explicit `--yes` for apply.
- Require `--allow-destructive` for delete/restore/shutdown/restart.

## Retry strategy

Retry only transient conditions:

- HTTP: 408, 425, 429, 500, 502, 503, 504
- network timeout/connection reset

Never auto-retry:

- 400, 401, 403, 404, 409, 422

## Drift and reconciliation

- Use report mode first.
- Guarded mode applies non-destructive changes.
- Enforce mode requires explicit destructive acknowledgment.

## Audit requirements

Every run should write JSONL audit entries including:

- timestamp
- run_id
- action/event name
- summary/result
- SHA-256 digest
