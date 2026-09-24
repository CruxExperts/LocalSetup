---
status: ACTIVE
version: 5.6
owner_skill: ls-architecture
---

# EnvMan integration contract

EnvMan is an independently released, opt-in per-user environment manager. LocalSetup observes it only through a redacted CLI capability probe; it does not install, update, vendor, configure, source, parse, or otherwise manage EnvMan.

## Activation and inheritance

The user activates EnvMan with its own `envman init` workflow and opens or reloads a configured shell. LocalSetup and its ordinary descendants then receive managed variables only through normal process-environment inheritance. EnvMan has no supported noninteractive `run` or `exec` command, so LocalSetup must not promise arbitrary command injection.

## Read-only capability probe

When explicitly selected, LocalSetup may discover `envman` through its generic executable-status mechanism, then run only:

```text
envman --version
envman check --json
```

The resulting non-secret state is `unavailable`, `present-unverified`, `available`, or `not-activated-or-empty`. It may report the executable path, version, EnvMan check target, count, and compatibility decision. It must not call `list`, `get`, `--reveal`, `set`, `unset`, import/export, backup, update, the TUI, or any shell-loader operation.

## Boundaries

LocalSetup must not read `environment.conf`, EnvMan release receipts, encrypted backups, `ENVMAN_BACKUP_KEY`, variable names, or values. It must not persist those items in logs, state, docs, plugin context, queue packets, context indexes, or telemetry.

The EnvMan installation/release protocol remains EnvMan-owned. A LocalSetup capability may bind only the observed CLI version to its own compatibility decision; it does not copy EnvMan receipt provenance.

Missing binaries, invalid JSON, nonzero checks, incompatible versions, unsafe stores, or shell-loader failures are nonfatal redacted capability states. On every probe failure, LocalSetup discards raw stdout and stderr without logging or returning either stream, then synthesizes only the bounded state and safe failure code. This prevents malformed EnvMan configuration diagnostics from leaking an assignment value. Failures leave the caller environment unchanged and do not trigger a fallback configuration-file read.

## Packaging

If shipped, this is an explicitly selected optional LocalSetup integration/skill record. It is not a baseline pack, generated plugin context input, vendored package, or automatic installer. Tests must mock probes and prove no mutation/value-oriented command is invoked and no names or values escape the public result.
