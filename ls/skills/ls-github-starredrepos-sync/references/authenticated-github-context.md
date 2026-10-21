# Authenticated GitHub Context

Start with:

```bash
node scripts/verify-github-auth.mjs
```

The script checks:

- `gh auth status --hostname <host>`
- `gh api /user`
- `gh api /versions`
- `gh api /rate_limit`
- a small GraphQL viewer query

Keep the output token-safe. Do not run `gh auth token` for display. If a downstream tool needs a token, let `gh` inject authentication through `gh api` instead.

If authentication is missing, ask the user to run `gh auth login` with the necessary scopes for reading stars and creating repositories.
