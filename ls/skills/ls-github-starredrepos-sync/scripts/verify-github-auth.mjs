#!/usr/bin/env node

import { GITHUB_HOST, REST_VERSION, ghApi, parseCommonArgs, run, usageAndExit } from "./common.mjs";

function usage() {
  return `Usage: node scripts/verify-github-auth.mjs [--json] [--help]

Verify GitHub CLI authentication and API context for starredrepos.

Options:
  --json    Print machine-readable JSON.
  --help    Show this help text.`;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), { "--json": "boolean" });
  if (options.help) {
    usageAndExit(usage());
  }

  await run("gh", ["auth", "status", "--hostname", GITHUB_HOST], { timeoutMs: 30000 });
  const user = await ghApi("/user");
  const versions = await ghApi("/versions");
  const rate = await ghApi("/rate_limit");
  const graphql = await run(
    "gh",
    [
      "api",
      "graphql",
      "--hostname",
      GITHUB_HOST,
      "-f",
      "query=query { viewer { login starredRepositories { totalCount } } rateLimit { limit cost remaining resetAt } }",
    ],
    { timeoutMs: 30000 }
  );
  const graph = JSON.parse(graphql.stdout);
  const payload = {
    host: GITHUB_HOST,
    restApiVersion: REST_VERSION,
    login: user.login,
    availableRestApiVersions: versions,
    starredRepositoryCount: graph.data?.viewer?.starredRepositories?.totalCount ?? null,
    rateLimit: rate.rate,
    graphqlRateLimit: graph.data?.rateLimit ?? null,
  };

  if (options.json) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } else {
    process.stdout.write(
      [
        `GitHub host: ${payload.host}`,
        `Authenticated user: ${payload.login}`,
        `REST API version header: ${payload.restApiVersion}`,
        `Available REST API versions: ${payload.availableRestApiVersions.join(", ")}`,
        `GraphQL starred repositories: ${payload.starredRepositoryCount}`,
        `REST core remaining: ${payload.rateLimit?.resources?.core?.remaining ?? "unknown"}`,
      ].join("\n") + "\n"
    );
  }
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
