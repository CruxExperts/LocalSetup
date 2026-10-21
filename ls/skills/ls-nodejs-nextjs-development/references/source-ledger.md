# Source Ledger

Version and release metadata were verified at `2026-09-03T06:17:06.012Z`.

## Package Metadata

- npm registry metadata for `next`: `https://registry.npmjs.org/next`
- npm registry metadata for `react`: `https://registry.npmjs.org/react`
- npm registry metadata for `react-dom`: `https://registry.npmjs.org/react-dom`
- npm package manifest fields: `https://docs.npmjs.com/cli/v11/configuring-npm/package-json`
- npm registry attestation endpoint pattern:
  `https://registry.npmjs.org/-/npm/v1/attestations/{package}@{version}`
- The verifier records package `time`, version `dist` integrity/tarball/signature
  fields, and registry attestation metadata; it does not verify artifact bytes.

## Node.js Runtime

- Node release schedule: `https://raw.githubusercontent.com/nodejs/Release/main/schedule.json`
- Node dist index: `https://nodejs.org/dist/index.json`
- Node previous releases: `https://nodejs.org/en/about/previous-releases`
- Node downloads: `https://nodejs.org/en/download`
- Node Corepack docs: `https://nodejs.org/api/corepack.html`

## Framework And Deployment

- Next.js docs: `https://nextjs.org/docs`
- Next.js releases: `https://github.com/vercel/next.js/releases`
- Next.js self-hosting docs: `https://nextjs.org/docs/app/guides/self-hosting`
- Next.js Edge Runtime docs: `https://nextjs.org/docs/pages/api-reference/edge`
- React versions/blog: `https://react.dev/versions` and `https://react.dev/blog`
- Vercel security changelog: `https://vercel.com/changelog?category=security`
- Docker Next.js container guide: `https://docs.docker.com/guides/nextjs/`

## Use Rules

- Re-run `../scripts/verify-current-versions.mjs` before relying on version
  facts for new work.
- Prefer primary documentation, release metadata, and package manifests over
  tutorial or aggregator content.
- Record the source URL and verification date next to any volatile claim added
  to this skill.
