import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  compareSnapshots,
  packageSnapshot,
  statusForLine,
} from "../scripts/verify-current-versions.mjs";

const HOUR_MS = 60 * 60 * 1000;

function metadata(publishedAt) {
  return {
    "dist-tags": {
      latest: "1.2.3",
      canary: "1.3.0-canary.1",
    },
    time: {
      "1.2.3": publishedAt,
    },
    versions: {
      "1.2.3": {
        engines: { node: ">=20" },
        peerDependencies: { react: "^19.0.0" },
        dist: {
          integrity: "sha512-example",
          shasum: "0123456789012345678901234567890123456789",
          tarball: "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
          signatures: [
            { keyid: "SHA256:first", sig: "first-signature" },
            { keyid: "SHA256:first", sig: "second-signature" },
          ],
          attestations: {
            url: "https://registry.npmjs.org/-/npm/v1/attestations/example@1.2.3",
            provenance: {
              predicateType: "https://slsa.dev/provenance/v1",
            },
          },
        },
      },
    },
  };
}

test("package snapshot preserves publication and supply-chain evidence", () => {
  const publishedAt = "2026-09-01T00:00:00.000Z";
  const insideWindow = packageSnapshot(
    "example",
    metadata(publishedAt),
    "2026-09-02T23:59:59.999Z",
  );
  const atBoundary = packageSnapshot(
    "example",
    metadata(publishedAt),
    "2026-09-03T00:00:00.000Z",
  );

  assert.equal(insideWindow.publication.ageAtVerificationMs, 48 * HOUR_MS - 1);
  assert.equal(insideWindow.publication.under48HoursAtVerification, true);
  assert.equal(atBoundary.publication.ageAtVerificationMs, 48 * HOUR_MS);
  assert.equal(atBoundary.publication.under48HoursAtVerification, false);
  assert.equal(atBoundary.supplyChain.integrity, "sha512-example");
  assert.deepEqual(atBoundary.supplyChain.signatures, [
    { keyid: "SHA256:first", sig: "first-signature" },
    { keyid: "SHA256:first", sig: "second-signature" },
  ]);
  assert.equal(atBoundary.supplyChain.signatureCount, 2);
  assert.deepEqual(atBoundary.supplyChain.signatureKeyIds, ["SHA256:first"]);
  assert.deepEqual(atBoundary.supplyChain.provenance, {
    url: "https://registry.npmjs.org/-/npm/v1/attestations/example@1.2.3",
    predicateType: "https://slsa.dev/provenance/v1",
  });
});

test("package snapshot rejects signatures without values", () => {
  const invalidMetadata = metadata("2026-09-01T00:00:00.000Z");
  invalidMetadata.versions["1.2.3"].dist.signatures[0].sig = "";
  assert.throws(
    () => packageSnapshot("example", invalidMetadata, "2026-09-02T00:00:00.000Z"),
    /dist.signatures\[0\]\.sig to be a non-empty string/,
  );
});

test("package snapshot rejects future publication timestamps", () => {
  assert.throws(
    () => packageSnapshot(
      "example",
      metadata("2026-09-03T00:00:00.001Z"),
      "2026-09-03T00:00:00.000Z",
    ),
    /publication time not to be in the future/,
  );
});

test("snapshot drift tracks 48-hour boundary but ignores raw age", () => {
  const stored = { packages: { next: { publication: {
    ageAtVerificationMs: 47 * HOUR_MS,
    under48HoursAtVerification: true,
  } } } };
  const current = { packages: { next: { publication: {
    ageAtVerificationMs: 49 * HOUR_MS,
    under48HoursAtVerification: false,
  } } } };

  assert.deepEqual(compareSnapshots(current, stored), [{
    path: "packages.next.publication.under48HoursAtVerification",
    before: true,
    after: false,
  }]);
});

test("Node status follows schedule boundaries", () => {
  const schedule = {
    start: "2026-01-01",
    lts: "2026-02-01",
    maintenance: "2026-03-01",
    end: "2026-04-01",
  };

  assert.equal(statusForLine("30", schedule, Date.parse("2025-12-31")), "Future (30.x)");
  assert.equal(statusForLine("30", schedule, Date.parse("2026-01-01")), "Current");
  assert.equal(statusForLine("30", schedule, Date.parse("2026-02-01")), "Active LTS");
  assert.equal(statusForLine("30", schedule, Date.parse("2026-03-01")), "Maintenance LTS");
  assert.equal(statusForLine("30", schedule, Date.parse("2026-04-02")), "EOL");
});

test("stored snapshot is internally consistent", async () => {
  const snapshot = JSON.parse(
    await readFile(new URL("../data/verified-versions.json", import.meta.url), "utf8"),
  );
  const verifiedAtMs = Date.parse(snapshot.verifiedAt);

  assert.equal("drift" in snapshot, false);
  assert.equal(snapshot.packages.next.latest, "16.3.4");
  assert.equal(snapshot.packages.react.latest, "19.2.8");
  assert.equal(snapshot.packages["react-dom"].latest, "19.2.8");
  assert.equal(snapshot.node.releaseLines["24"].latestDist, "v24.20.0");
  assert.equal(snapshot.node.releaseLines["26"].latestDist, "v26.8.1");

  for (const [name, entry] of Object.entries(snapshot.packages)) {
    const expectedAge = verifiedAtMs - Date.parse(entry.publication.publishedAt);
    assert.equal(entry.publication.ageAtVerificationMs, expectedAge, name);
    assert.equal(
      entry.publication.under48HoursAtVerification,
      expectedAge < 48 * HOUR_MS,
      name,
    );
    assert.match(entry.supplyChain.integrity, /^sha512-/u, name);
    assert.match(entry.supplyChain.shasum, /^[0-9a-f]{40}$/u, name);
    assert.equal(entry.supplyChain.signatures.length, entry.supplyChain.signatureCount, name);
    assert.ok(entry.supplyChain.signatures.every((signature) => signature.sig.length > 0), name);
    assert.ok(entry.supplyChain.signatureKeyIds.length > 0, name);
  }
});
