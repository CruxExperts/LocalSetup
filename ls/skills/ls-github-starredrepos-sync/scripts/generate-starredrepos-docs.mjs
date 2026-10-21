#!/usr/bin/env node

import { mkdir, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { defaultWorktree, parseCommonArgs, readJson, repoDocFileName, usageAndExit, validateManifest } from "./common.mjs";

function usage() {
  return `Usage: node scripts/generate-starredrepos-docs.mjs --manifest PATH [--out DIR] [--dry-run] [--help]

Generate README and per-repository markdown docs from a starredrepos manifest.

Options:
  --manifest PATH  Manifest JSON path.
  --out DIR        Output directory. Default: STARREDREPOS_WORKTREE or ~/starredrepos.
  --dry-run        Print planned files without writing.
  --help           Show this help text.`;
}

function repoDoc(repo) {
  return `# ${repo.fullName}

- URL: ${repo.htmlUrl}
- Starred: ${repo.starredAt || "unknown"}
- Language: ${repo.language || "unknown"}
- License: ${repo.license || "unknown"}
- Archived: ${repo.archived ? "yes" : "no"}

## Summary

${repo.description || "No repository description."}
`;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), {
    "--manifest": "value",
    "--out": "value",
    "--dry-run": "boolean",
  });
  if (options.help) {
    usageAndExit(usage());
  }
  if (!options.manifest) {
    throw new Error("--manifest is required");
  }
  const manifest = await readJson(options.manifest);
  validateManifest(manifest);
  const out = options.out || defaultWorktree();
  const files = [
    {
      path: join(out, "README.md"),
      content: `# Starred Repositories\n\nGenerated: ${manifest.generatedAt}\n\nRepository count: ${manifest.repositoryCount}\n`,
    },
    ...manifest.repositories.map((repo) => ({
      path: join(out, "docs", "repos", repoDocFileName(repo.fullName)),
      content: repoDoc(repo),
    })),
  ];
  if (options.dry_run) {
    process.stdout.write(`${JSON.stringify({ plannedFiles: files.map((file) => file.path) }, null, 2)}\n`);
    return;
  }
  for (const file of files) {
    await mkdir(dirname(file.path), { recursive: true });
    await writeFile(file.path, file.content, "utf8");
  }
  process.stdout.write(`${JSON.stringify({ writtenFiles: files.map((file) => file.path) }, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
