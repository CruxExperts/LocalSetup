# Backlog template

Use this template when creating the canonical backlog file at `.localsetup/backlog.md`. Never store backlog or reminder state under `ls/`; that directory is framework-owned source.

```markdown
# Backlog

*Last updated: YYYY-MM-DD*

## Overdue

- [ ] Short title (due: YYYY-MM-DD) optional note

## Due soon (today through 6 days after today)

- [ ] Short title (due: YYYY-MM-DD) optional note

## Scheduled (7 or more days after today)

- [ ] Short title (due: YYYY-MM-DD) optional note

## No date (whenever)

- [ ] Short title - optional note

## Done

- [x] Short title (done: YYYY-MM-DD)
```

## Path precedence

1. Use `.localsetup/backlog.md` as the canonical LocalSetup backlog.
2. Use root `BACKLOG.md` only when `.localsetup/backlog.md` does not exist or when the skill is running outside a LocalSetup repo.
3. If both files exist, update `.localsetup/backlog.md` and tell the user that `BACKLOG.md` is legacy.

## Timezone rule

Resolve relative due dates with one consistent timezone: explicit user or environment timezone, then `LOCALSETUP_TIMEZONE`, then `TZ`, then host local timezone. Ask once before writing when the date would be ambiguous. Store exact-time reminders only in a scheduler that can deliver them; conversion to this date-only backlog requires explicit user confirmation. Reclassify incomplete dated items on every read or update: overdue before today, due soon from today through six days after today, and scheduled seven or more days after today.
