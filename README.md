# LocalSetup

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/localsetup-readme-hero-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/localsetup-readme-hero.png">
    <img src="assets/localsetup-readme-hero.png" alt="LocalSetup (LS): One framework. Multiple AI agents. Your rules. Blue bracket mark and connected coder, research, operations, and creative agent cards on a midnight-blue space background." width="960">
  </picture>
</p>

### One framework. Multiple AI agents. Your rules.

**Give your AI coding agents a shared standard for how to work.**

Switching between Codex, Claude Code, Cursor, and other coding tools often means repeating instructions and explaining your development standards all over again.

LocalSetup (LS) is a free, open-source operating framework for consistent, standards-driven AI development. It brings your project context, coding standards, reusable skills, and development workflows into one organized system.

**Set your standards once. Reuse them across projects and supported AI agents.**

Your practices live in version-controlled files that you can review, maintain, and reuse beyond any single conversation. Your agents get a lasting reference for how you expect projects to be built, tested, and managed.

## A foundation for AI development

**Persistent project context.** Keep architectural decisions, coding conventions, and project instructions in a reviewable reference. Make useful knowledge available to future sessions through files your agents can read.

**Reusable skills and workflows.** A skill gives an agent instructions for a task. A workflow guides it through a series of steps. Choose from the library for coding, debugging, testing, documentation, reviews, infrastructure, security, and automation.

**Engineering standards in practice.** Give agents clear procedures for checking their work, documenting changes, and handling operations safely. Define your approval boundaries alongside your quality requirements.

**Consistent standards across tools.** Use a shared library with Claude Code, OpenAI Codex, Cursor, Kilo, OpenCode, GitHub Copilot, Gemini CLI, and other [supported clients](ls/docs/PLATFORM_REGISTRY.md). Select the packages each project needs while preserving your existing tools and custom configurations. Each client still follows its own loading rules and permissions.

**Work you can resume.** Use documented steps, checkpoints, verification, and handoffs to make complex tasks easier to pick up after an interruption or transfer to another agent or developer.

**Your environment, your control.** Preview installation changes, inspect managed state, and use recorded rollback information to restore managed paths when needed.

<details>
<summary>Library size and supported clients</summary>

<!-- facts-block:start -->
| Fact | Value |
|---|---|
| Current version | `4.53.1` |
| Supported platforms | `codex, claude-code, cursor, kilo, opencode, openclaw, github-copilot-cli, github-copilot-vscode, cline-cli, cline-vscode, amp-cli, goose-cli, pi-cli, hermes-agent, qwen-code-cli, kimi-cli, factory-droid, antigravity-app, gemini-cli, omp-cli` |
| Shipped skills | `113` |
| Workflow packages | `20` |
| Source | `ls/docs/_generated/facts.json` |
<!-- facts-block:end -->

</details>

## Put your standards to work

Start with a project you already maintain. Define its coding conventions, preferred architecture, testing requirements, documentation rules, and approval boundaries.

LocalSetup helps organize that knowledge alongside reusable capabilities and makes selected packages available to your supported agents. When you switch tools or start another project, reuse the relevant standards and add the instructions specific to that project.

That means less repeated setup and a clearer reference for everyone working on the code, whether you're developing independently, maintaining infrastructure, or collaborating across repositories.

**Your knowledge stays with your development environment, where you can review and improve it.**

## Part of the CruxExperts ecosystem

LocalSetup is part of the [CruxExperts open-source ecosystem](https://github.com/CruxExperts), a collection of independently useful tools for development and operations.

**[Envman](https://github.com/CruxExperts/EnvMan)** manages environment variables and sensitive configuration. LocalSetup supports optional installation and update checks while leaving Envman's private configuration under its own control.

**[bbackup (BestBackup)](https://github.com/CruxExperts/BestBackup)** provides encrypted Linux file backups, verified file restores, and a structured JSON command interface for automation and AI agents. It complements LocalSetup's development guidance as a separate tool.

The aim is to grow this collection around compatible practices for development, automation, security, and recoverable operations. LocalSetup provides a shared framework for those practices.

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

**Version:** 4.53.1<br>

<!-- release-summary:start -->
## What's new in 4.53.1

LocalSetup 4.53.1 gives AI coding agents a shared standard for how to work. The refreshed homepage explains how to keep development practices and useful project knowledge in reviewable files, reuse them across supported tools, and get started with less repeated setup.

- **Put your standards first:** the homepage introduces LocalSetup as an open-source operating framework for development standards, reusable knowledge, and consistent workflows. A practical project example shows how to use it. The existing hero artwork stays in place.
- **Keep the voice consistent:** the branding guide and repository instructions now call for plain language, concrete benefits, and clear explanations. Persistence means files that agents can read and review; each client still follows its own loading rules and permissions.
- **See how the tools fit together:** the homepage links to Envman and bbackup (BestBackup), explains their roles in the CruxExperts ecosystem, and distinguishes optional Envman integration from an independent backup tool.

See the [4.53.1 release guide](ls/docs/releases/4.53.1.md) for compatibility, updating, and verification.
<!-- release-summary:end -->

</details>

## Open source. Built for the long term.

LocalSetup is developed by [Crux Experts LLC](https://www.cruxexperts.com/) and released under the [MIT License](LICENSE). Use it with your preferred coding assistants and your existing model access.

Establish standards, preserve useful knowledge, and build a more consistent development environment around the AI tools you already use.

**Own your stack. Standardize your agents. Automate the work.**

For questions, start a [GitHub Discussion](https://github.com/CruxExperts/localsetup/discussions). For bugs and feature requests, see [Support](SUPPORT.md). Report security issues using [SECURITY.md](SECURITY.md).

Copyright © 2026 Crux Experts LLC. See [contribution and attribution notes](CONTRIBUTING.md#attribution).
