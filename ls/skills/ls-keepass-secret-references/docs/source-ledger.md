# Source Ledger

Access date: 2026-05-12.

| source | observed fact | impact | confidence | conflicts |
|---|---|---|---|---|
| Local repo ls/skills/ls-keepass-secret-references/SKILL.md | The previous skill was guidance-only and shipped no helper CLI. | This implementation refactors the existing skill instead of adding a duplicate. | High | None |
| Local repo ls/docs/INPUT_HARDENING_STANDARD.md | External input must be sanitized, validated, and handled with actionable errors. | CLI validates IDs, aliases, env names, and YAML roots; subprocess calls use shell=False. | High | None |
| Local repo pyproject.toml / uv.lock | PyYAML is already a framework dependency. | YAML config/map parsing uses PyYAML without adding a new dependency. | High | None |
| Local repo git status / git rev-parse | The plan baseline was captured from the active worktree. | Generated docs and tests are run against the active worktree, not memory. | Medium | Baseline may advance during local work. |
| Local Python | Python 3.12.3 was available in this environment. | CLI targets Python 3.12+ and is tested on the available local runtime. | High | Local runtime may change. |
| Local keepassxc-cli --version | Plan observed local KeePassXC CLI 2.7.12. | The default backend is a fail-closed capability guard; diagnostics report the local version without opening a vault. | Medium | CLI may not be installed in every environment. |
| KeePassXC downloads | KeePassXC publishes platform downloads and CLI distribution guidance. | Installation docs point users to official packages instead of bundling installers. | Medium | External packaging can change. |
| KeePassXC GitHub releases / 2.7.12 notes | 2.7.12 was the plan-observed local release. | Doctor/verification reports local version but does not upgrade. | Medium | Latest release can change after access date. |
| KeePassXC docs / Getting Started | KeePassXC uses KDBX databases and interactive unlock flows. | This package preserves that boundary: it never unlocks or operates a real KeePassXC database. | High | None |
| KeePassXC signature verification docs | Official downloads can be signature-verified. | Installation docs recommend verifying downloaded installers. | High | None |
| Arch keepassxc-cli man page | CLI supports command-oriented operations including show/add/edit patterns. | The package deliberately does not invoke those operations; its fake backend exercises only test fixtures. | Medium | Distribution man pages can vary by version. |
| KeePass KDBX 4 reference | KDBX is the KeePass database container format. | Threat model treats .kdbx as sensitive even when encrypted. | High | None |
| OWASP Key Management Cheat Sheet | Key material needs separation, rotation, and least exposure. | Config/map files reject embedded secret-like values. | High | None |
| NIST SP 800-57 Part 1 Rev. 5 | Key management requires lifecycle controls and protection of secret material. | Mapping validation avoids secret material; lifecycle operations remain outside this package. | High | None |
| PyKeePass docs | PyKeePass can manipulate KeePass files from Python. | PyKeePass is documented as a future optional backend, not added as a dependency. | High | None |
