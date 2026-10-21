# Rate Limits and Resilience

- Use `per_page=100` where supported.
- Follow REST `Link` pagination headers.
- Capture `x-ratelimit-limit`, `x-ratelimit-remaining`, `x-ratelimit-reset`, and `x-ratelimit-resource` when available.
- Keep GraphQL queries narrow and record `rateLimit` fields when used.
- On rate limit exhaustion, stop and report the reset time instead of retrying aggressively.
