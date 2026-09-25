---
status: ACTIVE
version: 5.7
owner_skill: ls-task-skill-matcher
---

# Frontend Web App Skill Routing

**Purpose:** Canonical LocalSetup routing for frontend web-app tasks that overlap
with the cached Codex Build Web Apps plugin. Use this document to choose native
LocalSetup skills without creating duplicate triggers or copying plugin corpora.

## Routing Principle

LocalSetup keeps source skills canonical. Plugin-covered concepts are retained
only as concise, LocalSetup-authored routing and workflow summaries inside
existing skills. Do not add duplicate top-level skills for the same trigger.

## Retention Matrix

| Cached plugin skill | LocalSetup target | Decision | Rationale |
|---|---|---|---|
| `frontend-app-builder` | `ls-web-artifacts-builder`, plus `ls-frontend-design` and `ls-ui-browser-debugging` | Retain selected concept-to-implementation workflow | Native artifact guidance now covers image-assisted concepting, accepted-design specs, design-system extraction, faithful implementation, and browser screenshot comparison. |
| `frontend-testing-debugging` | `ls-ui-browser-debugging` | Do not import | Browser QA, Chrome DevTools MCP, Playwright, evidence capture, and regression-test routing already live in the canonical LocalSetup skill. |
| `react-best-practices` | `ls-vercel-react-best-practices` | Summarize, do not import rule corpus | Native React guidance now emphasizes version/router inspection, server/client split, caching, render boundaries, hydration, bundle pressure, hot paths, and validation. |
| `shadcn` | `ls-shadcn-ui` | Do not import | LocalSetup already ships a stronger shadcn/ui skill with project-aware setup, CLI, registry, theming, forms, update, and troubleshooting coverage. |
| `stripe-best-practices` | `ls-stripe-payments` | Retain routing and source-check guidance | Native Stripe guidance now routes Checkout, Payment Element, Billing, Connect, and Treasury work while preserving secrets, webhooks, idempotency, and API-version verification. |
| `supabase-postgres-best-practices` | `ls-supabase` | Retain Postgres performance review prompts | Native Supabase guidance now covers query plans, indexes, RLS performance, query statistics, pooling, locks, transactions, and migration review. |

## Provenance And License Boundary

This routing review evaluated a cached OpenAI-curated Build Web Apps plugin
snapshot identified as plugin id `build-web-apps`, version `0.1.2`, snapshot
`3fdeeb49`. Its manifest indicates MIT licensing, but the reviewed cache did
not include the full license text. LocalSetup therefore does not bulk-copy
plugin skill bodies, rule files, assets, examples, or long passages. The
retained guidance is original LocalSetup-authored summary text backed by primary
documentation links.

## Adapter Exposure Guidance

Source/package availability is distinct from Codex global adapter exposure. If a
skill exists in `ls/skills/` but is not exposed by a particular Codex
profile, treat that as a deploy or profile selection issue. Use LocalSetup-native
attach, apply, install, or package tooling to expose the existing skill. Do not
create duplicate skills to work around adapter gaps.

## Primary Sources

- OpenAI image generation:
  - https://developers.openai.com/api/docs/guides/image-generation
  - https://developers.openai.com/api/reference/resources/images
- React, Next.js, and Vercel:
  - https://react.dev/reference/react
  - https://nextjs.org/docs/app/getting-started/server-and-client-components
  - https://nextjs.org/docs/app/guides/caching
  - https://vercel.com/docs/agent-resources/vercel-plugin
- shadcn/ui:
  - https://ui.shadcn.com/docs
  - https://ui.shadcn.com/docs/cli
  - https://ui.shadcn.com/docs/components-json
- Stripe:
  - https://docs.stripe.com/payments/checkout
  - https://docs.stripe.com/payments/payment-element
  - https://docs.stripe.com/connect
  - https://docs.stripe.com/billing
  - https://docs.stripe.com/treasury
  - https://docs.stripe.com/api/versioning
- Supabase:
  - https://supabase.com/docs/guides/database/query-optimization
  - https://supabase.com/docs/guides/database/postgres/indexes
  - https://supabase.com/docs/guides/database/debugging-performance
  - https://supabase.com/docs/guides/database/extensions/pg_stat_statements
  - https://supabase.com/docs/guides/troubleshooting/rls-performance-and-best-practices-Z5Jjwv
