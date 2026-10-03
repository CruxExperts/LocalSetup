---
status: ACTIVE
version: 4.45
owner_skill: ls-architecture
---

# EnvMan integration contract

EnvMan is an independently released, opt-in per-user environment manager. LocalSetup provides explicit lifecycle commands and redacted read-only status checks; ordinary LocalSetup commands never install or update it.

## Activation and inheritance

Installing EnvMan does not activate it. The user runs Envman's own `envman init` workflow and opens or reloads a configured shell. LocalSetup and its ordinary descendants then receive managed variables only through normal process-environment inheritance. EnvMan has no supported noninteractive `run` or `exec` command, so LocalSetup must not promise arbitrary command injection.

## Explicit commands

The optional command group is separate from LocalSetup's install, update, repair, doctor, preset, and `--sync-env` paths:

```bash
localsetup envman status
localsetup doctor --envman
localsetup envman update --check
localsetup envman install
localsetup envman update
```

`status` and `doctor --envman` are report-only. They discover the executable through LocalSetup's generic tool lookup and run only:

```text
envman --version
envman check --json
```

The check response is strictly validated against the known older and current field sets. LocalSetup drops its `target` field because it contains a local config path. It reports only the state `unavailable`, `present-unverified`, `available`, or `not-activated-or-empty`, a validated version, nonnegative counts, the encryption Boolean when supported, and a fixed failure code. Missing or unhealthy Envman remains nonfatal to doctor and is omitted unless `--envman` is supplied.

`localsetup envman update --check` is also read-only, but it explicitly asks the installed Envman command to check the latest release. It invokes only `envman update --check --json` and accepts only the fixed `envman.update-result` schema with schema version 1, a known status, and valid installed/available SemVer values. This check may access the network.

The explicit `install` and mutating `update` actions invoke Envman's official latest bootstrap once, using the available `uv` executable and CPython 3.12. There is no LocalSetup version pin and no automatic `envman init`:

```text
uv run --python 3.12 --script https://github.com/CruxExperts/envman/releases/latest/download/install.py
```

Envman's bootstrap supports Linux x86_64 with CPython 3.12 and `uv` 0.11 or newer. Other hosts receive a fixed unsupported-host result without a bootstrap attempt. The bootstrap is served from GitHub's `releases/latest` HTTPS download URL and is not itself pinned by a LocalSetup version or listed as a hashed payload in Envman's release manifest. The bootstrap resolves the latest release manifest and verifies the declared immutable payload URLs, sizes, and SHA-256 digests before installing those payloads. LocalSetup does not inspect Envman receipts or local configuration. When a requested skill install fails, Envman attempts to restore the prior skill, wheel, and receipt; restoration can itself fail, so LocalSetup reports a nonzero or timed-out mutation as `unknown`, suppresses all mutation output, and never retries automatically.

An Envman skill projection is optional and is passed only when `--install-skill` is explicitly present. It requires exactly one `--skill-scope repository|global` and one `--skill-target` from the current LocalSetup platform registry:

```bash
localsetup envman install --install-skill --skill-scope global --skill-target codex
localsetup envman update --install-skill --skill-scope repository --skill-target codex --target-directory /path/to/repository
```

Global scope does not accept a repository directory. Repository scope requires an explicit directory that resolves to its Git root. LocalSetup sets that root as the bootstrap process working directory, matching Envman's ancestor-based repository detection; a nested directory is rejected. `auto`, `all`, inferred scope, repeated targets, and implicit skill installation are not accepted.

## Boundaries

LocalSetup must not read `environment.conf`, EnvMan release receipts, encrypted backups, `ENVMAN_BACKUP_KEY`, variable names, or values as part of this toolchain integration. It must not persist those items in logs, state, docs, plugin context, queue packets, context indexes, or telemetry. This boundary does not change the separate caller-authorized OpenPGP secret resolver, which may use `envman get --json --reveal` only for an explicitly selected secret reference and keeps the resolved value out of its public result and command arguments.

The EnvMan installation/release protocol remains EnvMan-owned. LocalSetup does not copy EnvMan receipt provenance or Envman content into LocalSetup packages.

Missing binaries, invalid JSON, nonzero checks, incompatible versions, unsafe stores, and unsupported hosts produce fixed redacted states. Probe stdout is bounded; stderr and all raw output are discarded. Failures do not trigger a fallback configuration-file read. A failed or timed-out mutation has an uncertain outcome and is not retried.

## Packaging

This is an optional CLI integration, not a baseline pack, generated plugin context input, vendored package, or automatic installer. Tests mock subprocesses and prove that ordinary commands do not dispatch Envman lifecycle actions, secret-oriented commands are not used for status, and local paths or raw output do not escape the redacted result.
