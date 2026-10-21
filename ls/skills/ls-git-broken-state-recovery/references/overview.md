# Unfuck My Git State

## A `git reset HEAD --hard` Manifesto

Git is complicated AF — and I say that with love.

I’ve used git almost every day for 24 years, and it still humbles me with its unfathomable complexity. A week still does not go by without me learning one more 'neat trick' or one more way it can literally FUCK my entire day.

I don't care who you are - "oh you're that NASA flight engineer who wrote the navigational algorithm and tweaked the assembly code that landed the Curiosity robot on Mars?" - even you’ve had that moment after git reset --hard where your brain goes: “Did I just vaporize $134M in history… or am I just having a normal day?” Then you run git reflog and its literally more complex than the firehose of black box telemetry data from a Boeing crash.

Git: "You did *what* now? Oh, I gotchu. <prints you a wall of trickling Matrix runes> Hey, you have fun with that buddy!"

You consider the Nuclear Option: Deleting the entire repo on your local machine and re-cloning it. We don’t talk about the *Re-Clone*. It is the developer walk of shame. It is burning down your house because you saw a spider. Oh you've never done that? Fucking liar! We all just want to get back to main

I have this overwhelming sense of empathy when I look at this new wave of vibe coders and I’m like… buddy… you are not emotionally prepared for Git. They arrive thinking ooooh "save button, but spicy!" Then Git throws its first real problem at them and suddenly they’re staring into the abyss, learning what “detached HEAD” means like it’s a medical diagnosis.

That's why I published this new skill, "Unfuck My Git State". It’s basically an airbag for when Git decides to teach you humility.

## What This Is

A recovery skill for broken Git state:
- orphaned worktrees
- phantom "branch already used by worktree" locks
- detached or contradictory `HEAD`
- missing refs and weird object-name failures
- all-zero worktree hashes that scream "something is cursed"

## What This Is Not

- Not a license to spam `reset --hard` until feelings improve.
- Not a replacement for backups.
- Not magic. Just a good process, minus the hand-holding.

## Included

- `SKILL.md`: staged recovery playbooks and escalation flow
- `scripts/snapshot_git_state.py`: read-only diagnostics snapshot
- `scripts/guided_repair_plan.py`: non-destructive command planner by symptom
- `scripts/regression_harness.py`: disposable regression simulator for broken states
- `references/symptom-map.md`: symptom to fix routing
- `references/recovery-checklist.md`: preflight and verification gate

## Operating Entry Point

Use `SKILL.md` as the operational source of truth. It contains the current
Python entrypoints, playbooks, and verification gate; this reference stays
high-level so command guidance does not drift in two places.

## Vibe Check

The name is chaotic. The workflow is not.
