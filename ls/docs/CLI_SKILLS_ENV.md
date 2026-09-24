---
status: ACTIVE
version: 5.6
owner_skill: ls-framework-compliance
---

# CLI-based skills environment policy

**Purpose:** Environment policy for CLI-based skills, including pipx, PATH, health checks, and status artifacts.

## Scope

This document defines a standard environment contract for CLI-based skills in LocalSetup:

- How tools are installed (pipx vs venv vs system).
- How binaries are discovered on PATH.
- How health checks and self-healing are performed.
- How results are exposed to agents that can only see the filesystem (for example, tmux-only flows).

Scrapling is the reference implementation. Other CLI skills should follow the same patterns unless there is a strong reason to diverge.

## Installation strategy

- Prefer user-level `pipx` installs for CLI tools.
  - Example: `pipx install "scrapling[all]"`.
  - Avoid system-wide installs unless a tool absolutely requires them.
- When a CLI requires extras to function (for example, Scrapling's shell / fetchers), always install with the appropriate extras:
  - Good: `pipx install "scrapling[all]"` or `pipx install "scrapling[shell]"`.
  - Avoid bare `pipx install scrapling` when helpers depend on extras.
- Helper code should generate concrete pipx command plans rather than guessing:
  - Capture the exact command list (for example, `["pipx", "install", "scrapling[all]"]`) and run it via a single execution helper.

## PATH handling for pipx apps

- CLI helpers must not assume that pipx-managed apps are already on PATH.
- Before spawning any CLI process, helpers should:
  - Discover the expected pipx app directory for the current user (typically `~/.local/bin` on Linux).
  - Augment `os.environ["PATH"]` for the child process so that this directory is included.
- PATH changes should be local to the helper process:
  - Do not write shell profiles or global PATH settings.
  - Keep the behavior deterministic and reversible.

## Health checks

- Helpers must use supported commands for health checks.
  - For Scrapling, `scrapling --version` is not a valid option.
  - Prefer:
    - `scrapling --help` for a lightweight startup sanity check, or
    - A small `scrapling extract ...` dry run that is cleaned up after use.
- Health checks should return a structured result, not just a boolean:
  - Include the command run, return code, and stderr snippet.
  - Expose a `healthy` flag derived from these fields.

## Self-healing behavior

- If a CLI is missing or unhealthy, helpers should:
  - Propose or execute a user-level `pipx install` or `pipx upgrade` with the correct extras.
  - Re-run the health check after installation or upgrade.
  - Only fall back to sudo/system-wide installs when explicitly configured by maintainers.
- The agent-facing contract:
  - At most, users may be asked to enter their sudo password in an existing tmux session.
  - All other steps (plan construction, execution, and verification) should be automated.

## Status artifacts for tmux-only agents

- Long-running or opaque operations (for example, `pipx install`, `pipx list`, CLI health checks) must write status artifacts to disk.
- Status artifacts should:
  - Live in a predictable directory (for example, `ls/state/cli_status/` or next to the main output file).
  - Use a `.status.json` extension.
  - Contain at least:
    - The command list that was run.
    - The return code.
    - Short stdout and stderr snippets (truncated if necessary).
    - A timestamp.
- Helpers that write primary content files (Markdown, HTML, JSONL) should:
  - Also write a sibling `*.status.json` file next to the content.
  - Return both `output_path` and `status_path` so agents can find them without guessing.

## Reuse by other CLI skills

- New CLI-based skills should:
  - Reuse shared helpers for:
    - pipx detection and install plan generation.
    - PATH augmentation for pipx apps.
    - Writing `*.status.json` artifacts.
  - Document, in their SKILL docs, how they:
    - Install and upgrade their underlying CLI.
    - Perform health checks.
    - Expose results to agents via content files and status artifacts.

## Scrapling-specific notes

Scrapling is the reference implementation of this policy and adds a few concrete expectations:

- Required tooling:
  - Python 3.12+ on the host.
  - User-level `pipx` available on PATH, or the ability to bootstrap it.
  - Docker is optional and used only as an escape hatch when the host cannot run Scrapling directly.
- Environment variables:
  - `SCRAPLING_PIPX_BIN` (optional): override the pipx binary name when it is not simply `pipx`.
  - `SCRAPLING_DOCKER_IMAGE` (optional): override the default Scrapling Docker image.
- Installation examples (Ubuntu):
  - Userland-only (no sudo):
    - install `uv`
    - `uv tool install pipx`
    - `pipx ensurepath`
    - `pipx install "scrapling[all]"`
  - Sudo-capable servers:
    - `sudo apt update`
    - `sudo apt install -y pipx`
    - `pipx ensurepath`
    - `pipx install "scrapling[all]"`
- Test matrix guidance:
  - For long-lived hosts, maintain at least one test instance for each of:
    - Ubuntu LTS (for example 22.04, 24.04) with userland-only installs.
    - Ubuntu LTS with sudo-capable installs.
  - On each, verify:
    - `ensure_available(dry_run=True)` returns a sane install or upgrade plan.
    - `scrapling_self_test(mode="offline")` succeeds and writes a `*.status.json` artifact.
