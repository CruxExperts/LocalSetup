# LocalSetup

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/localsetup-readme-hero-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/localsetup-readme-hero.png">
    <img src="assets/localsetup-readme-hero.png" alt="LocalSetup: See the work. Keep control. The harness for your agent harness. A blue bracket mark, agent tools, visible terminal operations, and OpenPGP workflows on a bright technical canvas." width="960">
  </picture>
</p>

### See the work. Keep control.

**The harness for your agent harness.**

LocalSetup (LS) adds shared project context, reusable workflows, visible operations, and explicit control boundaries around the AI tools you already use. It helps you carry your standards between supported clients and inspect managed work outside the conversation.

**Visibility. Security. Privacy.** See live commands in an owned terminal companion. Define authority before sensitive operations. Use shared OpenPGP capabilities for supported signed and encrypted workflows. Keep private operational evidence separate from material intended for publication.

AI is a tool. Expertise determines what it multiplies. LocalSetup brings engineering discipline to AI-assisted work: clear scope, observable execution, tested procedures, and evidence you can review.

[Read the complete story](ls/docs/WHY_LOCALSETUP.md) · [Explore the capabilities](ls/docs/FEATURES.md) · [Understand the security boundaries](SECURITY.md)

## An operating layer around your tools

**Carry your context forward.** Keep project instructions, architectural decisions, and engineering standards in reviewable files. Reuse them across projects and supported clients instead of reconstructing them for each conversation.

**Reuse proven actions.** Selected skills and workflows give agents a practical reference for implementation, tests, documentation, infrastructure, and maintenance. Client loading rules and native permissions remain in force.

**See work as it happens.** LocalSetup's managed tmux workflow exposes live operations in persistent native panes, with protected observation and explicit input handoffs. It provides an operator view of work dispatched through that workflow—not surveillance of every client action. [Terminal capabilities and prerequisites](ls/docs/TMUX_TERMINAL_MODE.md).

**Control sensitive workflows.** Shared OpenPGP tools support explicit trust, signatures, encrypted envelopes, and protected artifacts. Installation does not enroll trust or encrypt every file. The optional integrated [LSCli runtime](ls/docs/LSCLI.md) provides its own qualified grants and access controls; external clients retain their security models.

**Keep improving between sessions.** Opt-in [heartbeat automation](ls/docs/HARNESS_AUTOMATION.md) can collect evidence and perform a configured review in a separate run. Scheduling and agent execution require explicit setup. Reusable context, maintenance records, and handoffs make ongoing work easier to resume.

<details>
<summary>Library size and supported clients</summary>

<!-- facts-block:start -->
| Fact | Value |
|---|---|
| Current version | `4.84.6` |
| Supported platforms | `codex, claude-code, cursor, kilo, opencode, openclaw, github-copilot-cli, github-copilot-vscode, cline-cli, cline-vscode, amp-cli, goose-cli, pi-cli, hermes-agent, qwen-code-cli, kimi-cli, factory-droid, antigravity-app, gemini-cli, omp-cli` |
| Shipped skills | `115` |
| Workflow packages | `21` |
| Source | `ls/docs/_generated/facts.json` |
<!-- facts-block:end -->

</details>

## Expertise behind the system

LocalSetup is developed by Crux Experts LLC, grounded in its founder's 35 years of hands-on engineering—from Netscape-era websites in 1995 through networks, protocol stacks, servers, data centers, and software. That includes more than nine years owning a web hosting company, growing it from a bootstrapped bedroom operation into an office with employees before selling it.

**AI is a force multiplier for that experience.** Continued research, maintenance, and custom integration keep the system useful as tools and requirements change. The value is a maintained operating foundation backed by practical engineering judgment.

## Useful service for small businesses

Start with the business, identify work worth improving, and measure the result. A focused integration should demonstrate time saved, fewer errors, or lower operating cost against a real baseline. LocalSetup supports that work; the service is the expertise to choose the right opportunity and deliver it responsibly.

For commercial inquiries and integration services, visit [Crux Experts](https://www.cruxexperts.com/). Read [the engineering and service philosophy](ls/docs/WHY_LOCALSETUP.md#practical-service-for-small-businesses).

## Part of the CruxExperts ecosystem

LocalSetup is part of the [CruxExperts ecosystem](https://github.com/CruxExperts), a collection of independently useful development and operations tools. Each project has its own licensing and operational scope.

**[Envman](https://github.com/CruxExperts/EnvMan)** manages environment variables and sensitive configuration. LocalSetup supports optional installation and update checks while leaving Envman's private configuration under its own control.

**[bbackup (BestBackup)](https://github.com/CruxExperts/BestBackup)** provides encrypted Linux file backups, verified file restores, and a structured command interface for automation. It is a complementary, separately managed tool.

## Get started

Use an interactive Bash terminal on Linux or macOS, or Windows through WSL2. You'll need Git, Python 3.12 or later, `uv`, and internet access to GitHub. See the [requirements](ls/docs/QUICKSTART.md#requirements) for details.

```bash
curl -sSL https://raw.githubusercontent.com/CruxExperts/localsetup/main/install | bash -s --
```

The guided installer explains the options, lets you select packages and supported AI clients, and previews changes before applying them.

- [Getting started](ls/docs/QUICKSTART.md)
- [Features and capabilities](ls/docs/FEATURES.md)
- [Skills catalog](ls/docs/SKILLS.md)
- [Supported AI platforms](ls/docs/PLATFORM_REGISTRY.md)
- [Workflow packages](ls/docs/WORKFLOW_PACKAGES.md)
- [All documentation](ls/docs/README.md)

## Release notes

[View the latest release](https://github.com/CruxExperts/localsetup/releases/latest).

<details>
<summary>Release included in this checkout</summary>

**Version:** 4.84.6<br>

<!-- release-summary:start -->
## What's new in 4.84.6

LocalSetup 4.84.6 adds a dynamic terminal workspace, private input dialogs, encrypted artifact workflows, and an installed agent runtime with bounded controls and recovery.

- **Check before scheduling:** the cron readiness command reports observable prerequisites, blockers, unchecked requirements, and next actions without running or enabling a task.
- **Review guidance before retirement:** assess accuracy, review age, historical value, dependent instructions, and the limits of a usage search before changing or retiring a reference.
- **Understand recovery confidence:** report backup attempts, integrity checks, and successful recovery exercises separately, then prepare an isolated exercise that protects originals.
- **Resume a contribution:** follow receiving-project rules and keep work, review readiness, submission readiness, and confirmed submission visible in one private progress record.
- **Continue settled LSCli history in another workspace:** select the source with `--source-workspace` and the destination with `--workspace`; the new session retains exact history while requiring fresh authority.
- **Explain an unfamiliar tool from source:** write an operational reference tied to a reviewed revision, with source citations, coverage, failure and recovery behavior, and visible unresolved questions.
- **Manage local previews safely:** guide existing project commands through verified loopback startup, reuse, inspection, conflict handling, and stopping only the owned preview.
- **Prepare media metadata copies:** choose hidden fields to retain, remove, or update, preserve original files and legitimate attribution, and verify each output against the chosen policy.
- **Prepare private transcripts and captions:** check local processing capability, mark drafts, review words and timing, export sidecar captions, and optionally render a separate captioned video.
- **Follow active work in the terminal:** dormant sections collapse to one row, active work expands below them, and paging preserves configurable minimum heights. Readable controls and selection redraws support keyboard and mouse use.
- **Enter private input through reusable dialogs:** text, multiline, password, checkbox, radio and confirmation fields use bounded, operation-bound delivery. Sudo and OpenPGP consumers retain their own authority checks.
- **Share encrypted files across carriers:** protected metadata, per-file messages, recipient addressing, volume reconstruction and persistent receipts preserve verification independently of storage or transport accounts.
- **Read protected files through ordinary tools:** configured catalogs resolve encrypted volumes and delta dependencies beneath explicit file and disclosure grants. Models do not select keys or bypass verification.
- **Preserve independent origins and concurrent edits:** bounded admissions retain each origin’s trust and receipts. Common-base forks remain separate until an explicitly authored merge creates a new immutable version.
- **Resume authorized snapshot publication:** exact coverage, current assignments and durable job identity govern client, worker and on-demand consolidation. Lost acknowledgements require reconciliation rather than duplicate publication.
- **Apply signed configuration at defined boundaries:** terminal tuning, container settings and ordered output filters consume validated revisions while preserving running operation identity.
- **Run coding tools behind explicit grants:** installed workers use the shared file, process and provider brokers. Private credentials stay outside worker environments, and cancellation remains available during stalled output.
- **Control and reconcile admitted runs:** one authenticated encrypted protocol carries bounded requests over owned streams, SSH relay processes and HTTP adapters. A fresh read-only recovery session can inspect an original run without replaying it.
- **Prepare installed heartbeat reviews:** explicit profiles, secret references, readiness checks and granted tool-free review support scheduled observation without treating configuration as activation authority.
- **Install verified runtime assets:** authenticated release sets and hash-locked dependency preparation support offline installation, protected launchers and retained predecessors.
- **Bound retained observations and provider context:** ordered filter policies, retained event limits and explicit context selection govern what resumed runs see, while preserving signed protocol fields and raw files.
- **Qualify terminal startup before dispatch:** status-pane startup tolerates a short process-label transition while retaining exact pane, process-birth and command checks. Native CI uses the qualified tmux runner and rejects an unexpected tmux version before the suite.

See the [4.84.6 release guide](ls/docs/releases/4.84.6.md) for compatibility, updating, and verification.
<!-- release-summary:end -->

</details>

## Available source. Experienced support.

LocalSetup is developed by [Crux Experts LLC](https://www.cruxexperts.com/). Current first-party source is free for noncommercial use under [PolyForm Noncommercial 1.0.0](LICENSE); commercial use requires a separate license. See [licensing scope](ls/docs/LICENSING.md) for earlier MIT grants and separately licensed components. Commercial inquiries are welcome through the company website.

Establish standards, preserve useful knowledge, and build a more consistent development environment around the AI tools you already use.

**Own your stack. Standardize your agents. Automate the work.**

For questions, start a [GitHub Discussion](https://github.com/CruxExperts/localsetup/discussions). For bugs and feature requests, see [Support](SUPPORT.md). Report security issues using [SECURITY.md](SECURITY.md).

Copyright © 2026 Crux Experts LLC. See [contribution and attribution notes](CONTRIBUTING.md#attribution).
