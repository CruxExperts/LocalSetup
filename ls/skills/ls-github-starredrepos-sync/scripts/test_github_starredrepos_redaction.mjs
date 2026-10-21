#!/usr/bin/env node

import { redact } from "./common.mjs";

const samples = [
  `ghp_${"a".repeat(36)}`,
  `gho_${"b".repeat(36)}`,
  `ghu_${"c".repeat(36)}`,
  `ghs_${"d".repeat(36)}`,
  `ghr_${"e".repeat(36)}`,
  `github_pat_${"f".repeat(36)}_${"g".repeat(24)}`,
  `Bearer ${"h".repeat(36)}`,
];

for (const sample of samples) {
  const redacted = redact(sample);
  if (redacted.includes(sample)) {
    throw new Error(`Token was not redacted: ${sample.slice(0, 12)}...`);
  }
}

process.stdout.write("ok\n");
