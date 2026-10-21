#!/usr/bin/env node

import { readdir } from "node:fs/promises";
import { join, resolve } from "node:path";
import {
  here,
  parseCommonArgs,
  readJson,
  usageAndExit,
  validateManifest,
  validateRepoMetadata,
  validateScoutReport,
} from "./common.mjs";

function usage() {
  return `Usage: node scripts/verify-starredrepos-state.mjs [--examples] [--manifest PATH] [--help]

Validate starredrepos example fixtures or a manifest file with stdlib shape checks.

Options:
  --examples       Validate bundled data/examples/*.json.
  --manifest PATH  Validate a manifest file.
  --help           Show this help text.`;
}

async function validateExamples() {
  const root = resolve(here(import.meta.url), "..");
  const examples = join(root, "data", "examples");
  const files = await readdir(examples);
  const checked = [];
  for (const file of files.filter((name) => name.endsWith(".json")).sort()) {
    const path = join(examples, file);
    const payload = await readJson(path);
    if (file.startsWith("repo-metadata")) {
      validateRepoMetadata(payload);
    } else if (file.startsWith("manifest")) {
      validateManifest(payload);
    } else if (file.startsWith("scout-report")) {
      validateScoutReport(payload);
    } else if (file.startsWith("snapshot-diff")) {
      if (payload.schemaVersion !== "1.0" || !Array.isArray(payload.added)) {
        throw new Error(`Invalid snapshot diff example: ${path}`);
      }
    }
    checked.push(path);
  }
  return checked;
}

async function main() {
  const options = parseCommonArgs(process.argv.slice(2), {
    "--examples": "boolean",
    "--manifest": "value",
  });
  if (options.help) {
    usageAndExit(usage());
  }
  const checked = [];
  if (options.examples) {
    checked.push(...(await validateExamples()));
  }
  if (options.manifest) {
    validateManifest(await readJson(options.manifest));
    checked.push(options.manifest);
  }
  if (!checked.length) {
    throw new Error("Nothing to validate; pass --examples or --manifest PATH");
  }
  process.stdout.write(`${JSON.stringify({ ok: true, checked }, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
