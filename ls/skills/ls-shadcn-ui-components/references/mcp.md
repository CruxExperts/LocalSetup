# MCP

Use MCP when the user asks for editor or agent integration with shadcn component
discovery or registry workflows. Verify current client behavior with official
docs and live help before changing user-global configuration.

## Current Client Targets

Live help has included:

```bash
<runner> shadcn@latest mcp init --client claude
<runner> shadcn@latest mcp init --client cursor
<runner> shadcn@latest mcp init --client vscode
<runner> shadcn@latest mcp init --client codex
<runner> shadcn@latest mcp init --client opencode
```

Verify the current client list with `mcp init --help`.

## Codex Setup

Official shadcn MCP docs currently state that the CLI cannot automatically edit
`~/.codex/config.toml`. For Codex, treat `mcp init --client codex` as guidance
or a helper, then inspect the official docs and add the TOML config manually if
the user requested Codex integration.

## Safety

- Inspect generated config before committing anything.
- Avoid storing secrets directly in MCP config.
- Prefer environment variables for private registry headers and params.
- Keep repo-local config separate from user-global config unless the user asks
  for global installation.
