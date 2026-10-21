#!/usr/bin/env node

import { fetchStarredRepos, parseCommonArgs, usageAndExit } from "./common.mjs";

function usage() {
  return `Usage: node scripts/list-starred-repos.mjs [--limit N] [--json] [--help]

List authenticated GitHub starred repositories with star timestamps when available.

Options:
  --limit N  Maximum repositories to return. Default: 100.
  --json     Print full JSON manifest-style payload.
  --help     Show this help text.`;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), {
    "--limit": "value",
    "--json": "boolean",
  });
  if (options.help) {
    usageAndExit(usage());
  }
  const limit = Number(options.limit || 100);
  if (!Number.isInteger(limit) || limit < 1 || limit > 10000) {
    throw new Error("--limit must be an integer between 1 and 10000");
  }
  const repositories = await fetchStarredRepos(limit);
  const payload = {
    schemaVersion: "1.0",
    generatedAt: new Date().toISOString(),
    repositoryCount: repositories.length,
    repositories,
  };
  if (options.json) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } else {
    for (const repo of repositories) {
      process.stdout.write(`${repo.fullName} ${repo.htmlUrl}\n`);
    }
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
