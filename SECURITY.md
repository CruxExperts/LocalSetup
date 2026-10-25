# Security

LocalSetup manages agent context, skills, install paths, and automation helpers. Please report security-sensitive issues privately so they can be fixed before public disclosure.

## Reporting a vulnerability

1. Do not open a public Issue with exploit details, secrets, hostnames, logs, or private paths.
2. Use GitHub private vulnerability reporting for this repository if it is enabled.
3. If private reporting is not available, use the security contact request issue form, but do not include vulnerability details, exploit steps, secrets, hostnames, private paths, or sensitive logs.

Include the affected file or command, LocalSetup version, platform ID if relevant, reproduction steps, expected impact, and whether the issue affects install behavior, skill import, agent permissions, generated docs, package boundaries, or release artifacts.

## Scope

This policy covers:

- Root install scripts and install tooling
- `ls/tools/`, `ls/lib/`, and `ls/config/`
- Shipped skills under `ls/skills/`
- Public templates, docs, package metadata, and generated release artifacts

Out of scope:

- Vulnerabilities in third-party agent hosts, shells, editors, Git, Python, operating systems, or user-provided projects
- Unsafe behavior introduced by locally modified or third-party skills after import
- Secrets or private data that users add to their own repositories

## Security model

Visibility, explicit authority, and deliberate disclosure guide the framework.
The managed tmux workflow provides an operator view of its commands; it is not
a sandbox or a monitor of every external client action. Shared OpenPGP services
support selected signed and encrypted workflows with explicit identity and
trust decisions. Installation does not activate trust or encrypt all project
files, terminal output, logs, or model traffic.

The integrated LSCli runtime's grants and brokered access apply to its qualified
execution paths. External coding clients retain their own permissions, provider
contracts, and security behavior. Scheduled collection and agent review are
opt-in. See [OpenPGP](ls/docs/OPENPGP_RUNTIME.md),
[terminal mode](ls/docs/TMUX_TERMINAL_MODE.md), and
[the operating-layer narrative](ls/docs/WHY_LOCALSETUP.md) for the scope of these
capabilities.

- Framework source is repo-local and reviewable.
- Managed skill installs are recreated from `ls/skills/`.
- Generated adapter paths are install output, not source of truth.
- Third-party skills must be treated as untrusted until vetted and normalized.
- Privileged server workflows use owned operation controls, explicit authority, and qualified authentication readiness.
- Secrets, credentials, private state, and machine-specific files must not be committed.

## Public disclosure

Please allow reasonable time for triage and a fix before publishing technical details. Security fixes should include verification steps and, when relevant, docs updates that explain the safer workflow.

## Repository security settings

Maintainers should keep Dependabot security updates, secret scanning, push protection, CodeQL default setup, and private vulnerability reporting enabled when GitHub makes those settings available for this repository. The maintainer checklist lives in [ls/docs/REPO_MAINTENANCE.md](ls/docs/REPO_MAINTENANCE.md).
