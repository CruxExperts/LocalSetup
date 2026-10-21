#!/usr/bin/env node

import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import {
  GITHUB_HOST,
  REPO_NAME,
  defaultWorktree,
  ensureGitRepository,
  fetchStarredRepos,
  ghApi,
  githubEnv,
  isoNow,
  parseCommonArgs,
  run,
  usageAndExit,
  validateManifest,
  writeJson,
} from "./common.mjs";

function usage() {
  return `Usage: node scripts/sync-starredrepos.mjs [--dry-run] [--apply] [--create-remote] [--commit] [--push] [--help]

Plan or apply a guarded starredrepos archive synchronization.

Options:
  --dry-run        Preview only. This is the default.
  --apply          Write local manifest and snapshot files.
  --create-remote  Create the archive remote if missing. Requires --apply.
  --commit         Create a local git commit. Requires --apply.
  --push           Push the commit. Requires --apply --commit.
  --help           Show this help text.`;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), {
    "--dry-run": "boolean",
    "--apply": "boolean",
    "--create-remote": "boolean",
    "--commit": "boolean",
    "--push": "boolean",
  });
  if (options.help) {
    usageAndExit(usage());
  }
  if (options.create_remote && !options.apply) {
    throw new Error("--create-remote requires --apply");
  }
  if (options.commit && !options.apply) {
    throw new Error("--commit requires --apply");
  }
  if (options.push && !(options.apply && options.commit)) {
    throw new Error("--push requires --apply --commit");
  }

  const storageMode = process.env.STARREDREPOS_STORAGE_MODE || "metadata";
  if (storageMode !== "metadata") {
    throw new Error(`Unsupported storage mode: ${storageMode}. Current helper apply mode is metadata-only; use STARREDREPOS_STORAGE_MODE=metadata.`);
  }
  const user = await ghApi("/user");
  const repositories = await fetchStarredRepos();
  const manifest = {
    schemaVersion: "1.0",
    generatedAt: isoNow(),
    owner: user.login,
    sourceHost: GITHUB_HOST,
    storageMode,
    repositoryCount: repositories.length,
    repositories,
  };
  validateManifest(manifest);

  const worktree = defaultWorktree();
  const summary = {
    mode: options.apply ? "apply" : "dry-run",
    worktree,
    remote: `${user.login}/${REPO_NAME}`,
    createRemote: Boolean(options.create_remote),
    commit: Boolean(options.commit),
    push: Boolean(options.push),
    repositoryCount: repositories.length,
    storageMode,
  };

  if (!options.apply) {
    process.stdout.write(`${JSON.stringify({ summary, manifest }, null, 2)}\n`);
    return;
  }

  await mkdir(worktree, { recursive: true });
  if (options.create_remote || options.commit) {
    await ensureGitRepository(worktree);
  }
  await writeJson(join(worktree, "manifest.json"), manifest);
  await writeJson(join(worktree, "snapshots", "latest.json"), manifest);

  if (options.create_remote) {
    await run(
      "gh",
      ["repo", "create", `${user.login}/${REPO_NAME}`, "--private", "--source", worktree, "--remote", "origin"],
      {
        env: githubEnv(),
        timeoutMs: 30000,
      }
    );
  }
  if (options.commit) {
    await run("git", ["add", "manifest.json", "snapshots/latest.json"], { cwd: worktree, timeoutMs: 30000 });
    await run("git", ["commit", "-m", "chore: sync starred repositories"], { cwd: worktree, timeoutMs: 30000 });
  }
  if (options.push) {
    const pushArgs = options.create_remote ? ["push", "--set-upstream", "origin", "HEAD"] : ["push"];
    await run("git", pushArgs, { cwd: worktree, timeoutMs: 30000 });
  }

  process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
