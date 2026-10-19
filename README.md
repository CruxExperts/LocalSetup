# Standalone skill contribution

Two Agent Skills packages preserve an operational result and its capture policy:

- `ls-public-https-origins`: public HTTPS identity, local Unix origins, optional
  bounded TLS relay and socket publication helpers.
- `ls-skill-capture-handoff`: separate capture/submission offers and complete
  compressed source handoff, with deterministic allowlist-based packaging.

## Standalone installation

Copy each complete named directory to the host's canonical global skills
library, or reference its maintained source using a symlink supported by the
agent host. Never overwrite an existing managed package. Both packages use
Python 3.12+ standard library only. Linux/root/systemd are needed only when
deploying the optional Unix socket bridge. Loading a skill performs no service
changes. Review and explicitly adopt the optional global policy separately.

## LocalSetup integration

Baseline: v4.49.2, commit `c0ecf4618255347615f52c5779b7f3a284048209`.
These are procedural skills, registered under `ls/skills/`, not separate
`ls/workflows/` packages. LocalSetup installation is not a runtime dependency.

From this contribution directory, preview the exact registration changes:

```sh
python3 integrate_localsetup.py --source-root /path/to/LocalSetup
```

The helper checks pack/taxonomy/smoke anchors and refuses name collisions. After
review and a recovery checkpoint in the owning checkout, add `--apply` to copy
the complete packages and update their owning registration metadata. It does
not modify global policy, generated catalogs, versions, releases or adapters.
If any write fails, inspect the partial diff before recovery; never blindly
repeat a mutation. Then run the current release's documented generation:

```sh
uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
uv run --locked python ls/tools/localsetup.py --source-root . validate-catalog
uv run --locked python ls/tools/localsetup.py --source-root . validate-package-surface
```

Inspect the focused diff and run project-required checks before acceptance.
No release bump or upstream integration is performed by this candidate.
The optional policy belongs in the existing authoring/completion owner after
maintainer review; it must not silently opt installations into global behavior.

## Archive and license

The attached tar.gz includes these source files and a per-file SHA-256
`MANIFEST.json`. Verify its outer digest from the issue, inspect member names
before extracting into an empty directory, then verify the manifest. Never
extract an untrusted archive over a working checkout. All contribution files
are released under the included MIT license.
