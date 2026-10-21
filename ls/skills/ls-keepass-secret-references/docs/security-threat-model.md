# Security Threat Model

Primary risks:

- Secret values accidentally written to tracked files.
- Secret values leaked in stdout, stderr, logs, or long-lived artifacts.
- Untrusted IDs or paths passed to subprocesses.
- Keyfiles or database backups committed to Git.

Controls:

- JSON output redacts sensitive fields by default.
- `--show-sensitive` is required for protected values.
- Config and map files reject secret-like values.
- Write operations require `--apply`.
- KeePassXC subprocess calls use `shell=False`.
- `.kdbx`, keyfile, and environment file patterns are ignored at the root.
