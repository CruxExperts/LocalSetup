# Source Ledger

Accessed: 2026-09-02.

| Topic | Source | Notes |
|---|---|---|
| REST API versions | https://docs.github.com/en/rest/about-the-rest-api/api-versions | 2026-03-10 and 2022-11-28 are supported; missing version headers default to 2022-11-28. |
| Starring REST API | https://docs.github.com/en/rest/activity/starring | `GET /user/starred` supports `per_page` up to 100 and the star timestamp media type. |
| REST authentication | https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api | Use authenticated context for private visibility and user stars; do not print tokens. |
| REST rate limits | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api | Record limit, remaining, reset, and resource when available. |
| REST pagination | https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api | Follow `Link` headers; do not depend on GitHub CLI `--slurp` because local `gh 2.45.0` lacks it. |
| Repositories REST API | https://docs.github.com/en/rest/repos/repos | Archive remote creation and repository metadata should use explicit version headers. |
| Releases REST API | https://docs.github.com/en/rest/releases/releases | Release intelligence is optional metadata and must mark missing release data as unknown. |
| GraphQL API | https://docs.github.com/en/graphql | Useful for cross-checking viewer identity and star counts. |
| GraphQL rate limits | https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api | GraphQL points are separate from REST limits; keep queries narrow. |
| GitHub CLI api | https://cli.github.com/manual/gh_api | Current docs include `--slurp`; this skill avoids it for compatibility with local `gh 2.45.0`. |
| GitHub CLI auth | https://cli.github.com/manual/gh_auth_status | `gh auth status` is the first local authentication check. |
| GitHub CLI repo | https://cli.github.com/manual/gh_repo_create | Remote creation must be opt-in with `--create-remote`. |
| Git submodules | https://git-scm.com/docs/git-submodule | Submodule storage is a roadmap concept only; current helper apply mode is metadata-only and rejects non-metadata storage modes. |
| Git clone | https://git-scm.com/docs/git-clone | Local checkout and bare mirror caches belong outside committed archive history. |
| GitHub Actions token | https://docs.github.com/en/actions/security-guides/automatic-token-authentication | Repository-scoped `GITHUB_TOKEN` is often insufficient for user-star synchronization. |
| Third-party Actions pins | https://docs.github.com/en/actions/reference/security/secure-use#using-third-party-actions | Full commit SHAs are the immutable form; template refs resolve through the official actions repositories and retain tag comments. |
| Node.js releases | https://nodejs.org/en/about/previous-releases | Target the supported Node 22.x LTS line for built-in fetch and maintained runtime behavior; it is scheduled through 2027-04-30. |

## Planning Verification Pattern

- Verify `gh auth status --hostname <host>` before relying on account context.
- Verify `gh api /versions` before assuming REST API version availability.
- Verify GraphQL star counts only as a run-local cross-check.
- Verify whether `<owner>/starredrepos` exists before planning remote creation.

Re-check volatile facts before relying on them in a later run.
