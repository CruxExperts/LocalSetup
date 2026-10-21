# Update Procedure

1. Re-check the source ledger against official GitHub, Git, GitHub CLI, and Node docs.
2. Run all script help commands.
3. Run example validation:

   ```bash
   node scripts/verify-starredrepos-state.mjs --examples
   ```

4. Refresh generated docs artifacts from the repo root:

   ```bash
   uv run --locked python ls/tools/generate_docs_artifacts.py --repo-root .
   uv run --locked python ls/tools/localsetup.py --source-root . generate-docs
   ```

5. Run the repo validation gates listed in the root AGENTS.md task instructions.
