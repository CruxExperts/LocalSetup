#!/usr/bin/env node

import { readFile } from "node:fs/promises";
import { parseCommonArgs, parseJson, run, usageAndExit, validateRepoMetadata, validateScoutReport } from "./common.mjs";

function usage() {
  return `Usage: node scripts/scout-repo-metadata.mjs --input PATH [--mode static|command] [--json] [--help]

Generate a schema-shaped scout report from repository metadata.

Options:
  --input PATH       JSON repository metadata file.
  --mode MODE        static or command. Default: STARREDREPOS_SCOUT_MODE or static.
  --json             Print JSON. Default output is also JSON for scriptability.
  --help             Show this help text.`;
}

function staticReport(repo) {
  const claims = [];
  if (repo.language) {
    claims.push({ text: `Primary language is ${repo.language}.`, status: "verified", evidence: "Repository metadata language field." });
  }
  if (repo.license) {
    claims.push({ text: `License metadata is ${repo.license}.`, status: "verified", evidence: "Repository metadata license field." });
  }
  if (repo.archived) {
    claims.push({ text: "Repository is archived.", status: "verified", evidence: "Repository metadata archived field." });
  }
  return {
    schemaVersion: "1.0",
    generatedAt: new Date().toISOString(),
    fullName: repo.fullName,
    mode: "static",
    summary: repo.description || `${repo.fullName} has no repository description.`,
    fit: repo.topics.length ? `Topics: ${repo.topics.join(", ")}` : null,
    risks: repo.archived ? ["Repository is archived."] : [],
    nextSteps: ["Review upstream README, license, and recent activity before deeper use."],
    claims,
  };
}

async function commandReport(repo) {
  const command = process.env.STARREDREPOS_SCOUT_COMMAND;
  if (!command) {
    throw new Error("STARREDREPOS_SCOUT_COMMAND is required for command scout mode");
  }
  const [bin, ...args] = command.split(/\s+/).filter(Boolean);
  const timeoutMs = Number(process.env.STARREDREPOS_SCOUT_TIMEOUT_MS || 30000);
  const result = await run(bin, args, {
    input: JSON.stringify(repo),
    timeoutMs,
  });
  const report = parseJson(result.stdout, "scout command");
  validateScoutReport(report);
  return report;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), {
    "--input": "value",
    "--mode": "value",
    "--json": "boolean",
  });
  if (options.help) {
    usageAndExit(usage());
  }
  if (!options.input) {
    throw new Error("--input is required");
  }
  const repo = parseJson(await readFile(options.input, "utf8"), options.input);
  validateRepoMetadata(repo);
  const mode = options.mode || process.env.STARREDREPOS_SCOUT_MODE || "static";
  const report = mode === "command" ? await commandReport(repo) : staticReport(repo);
  validateScoutReport(report);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
