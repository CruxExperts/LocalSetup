# Architecture

localsetup_secrets.py is a JSON-first validator for logical secret IDs. It resolves config, loads a non-secret map, resolves aliases, parses references, and dispatches only when a test fixture requires the fake backend.

Backends:

- keepassxc: default capability guard. It detects keepassxc-cli for diagnostics only. Backend creation reports missing_backend when the CLI is unavailable; otherwise every read and write operation fails with interactive_backend_required. The package never opens a real vault or automates unlock handling.
- fake: deterministic file-backed test backend for CI and examples. It must not be used as a real secret store.

Every command returns an envelope with ok, command, data, warnings, errors, sources, sensitive, and redactions.
