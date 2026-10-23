---
status: ACTIVE
version: 4.66
owner_package: ls-wf-tmux-terminal-mode
---

# Observable agent shells and tmux terminal mode

LocalSetup provides ordinary agent command sessions and an optional launcher for
IDE or interactive terminals. Use the ordinary shell API for transparent command
observation and recovery; use the existing protected operations workflow for
privileged work. Native permissions and sandbox boundaries remain effective.

## Observable commands and application profiles

`localsetup shell --help` describes the command lifecycle. It creates or joins a
verified agent-owned session on the execution machine and gives each command a
fresh shell in a pane of the root agent's family window. The session remains for
the root agent's lifespan; admitted subagents share it instead of creating their
own observation sessions. Concurrent panes stack with horizontal dividers at an
120-column default width and equalize as commands start and finish. Stable handles resolve
existing work after a disconnect; attachment never creates a replacement command. Completion, captured
output and cleanup are reported separately.

The launcher also removes its `LOCALSETUP_GLOBAL_SHIM`, `LOCALSETUP_SOURCE_ROOT`,
and `LOCALSETUP_HOME` invocation markers before starting a workload. A nested CLI
therefore resolves its own source and home instead of inheriting the controller's
checkout. Explicit command environment overrides remain available.

The initial command backend requires Linux process identity from `/proc` and a
qualified tmux installation. Other environments receive an unavailable result.
The family retained-text contract is currently qualified for tmux 3.4. Other
versions receive an explicit unqualified result for family creation; generic
legacy availability does not establish that contract.
Mutations require a verified long-lived agent PID and process start time, supplied
explicitly or through the documented owner environment variables. A short-lived
CLI or helper process is not the agent owner. If the native owner cannot be
qualified, preserve existing work and use observation rather than claiming control.

Agents announce whether they created or joined the session, identify its machine,
and show its local join command at startup, command start and at least once every
five minutes during long-running work. Keep it discoverable in the owned session
progress messages so a returning observer can find it without searching old messages.
The user runs that command on the named machine; it contains no SSH assumption.
Completed operations remain inspectable through retained status and logs after
their command panes have been removed.
Attachment requires an interactive terminal and is read-only. It neither grants
command-input authority nor creates a session. Authorized agent input and
cancellation use their operation handles through the shell API.

Use a stable agent ID alongside the long-lived process binding when recovery
must survive a new agent process. After the previous owner exits,
`recover --adopt` can transfer a verified session and its supervised running
commands to the replacement owner without replaying them. Unknown ownership,
operation records, pane occupants or observers block adoption. A different agent
ID does not silently take over another agent's session.

### Root and subagent membership

New sessions default to `ensure --layout family`. The root uses `shell admit`
with `--member-agent-id` and a short `--label`, then passes the returned one-time
invitation privately to that subagent. The subagent calls `shell join --handle`
with `--invite` and its own live owner binding. Membership does not authorize
sibling operations or family administration. The root remains responsible for
the shared session and topology.

Pane labels use the admitted identity and operation ID as a purpose slug:
`root · tests` or `subagent scout · source-map`. Use meaningful operation IDs;
command arguments, paths and application-provided titles are not identity labels.
Existing legacy sessions keep their layout and handles. `--layout legacy`
remains available for that compatibility path; requesting a different layout
does not silently convert an existing session.

To replace a closed legacy runtime while its viewer remains, explicitly close
that idle handle and use `ensure --replace-retiring` with the same root identity.
The new family receives a new handle. Old viewers keep their retired view and
old operation logs remain available; their eventual detach cannot remove the
new runtime. Only one generation accepts new work.

Application profiles are schema-validated JSON with shipped defaults and private
saved preferences. `localsetup app-profile --help` exposes resolution and origin
reporting. The tmux profile keeps the bottom session/window bar visible,
enables the mouse and requests 20,000 history lines. Family sessions report the
effective native capacity separately: the byte budget can reduce that line count.
The default qualified family capacity is 6,000 lines at 120 columns. The runtime
profile accepts widths from 80 to 160 columns and derives a safe history ceiling
from the selected width and retained-text budget. Its maximum canvas height
is the smaller of 512 rows and one tenth of that effective history limit, so a
100-line preference permits at most 10 rows. The first qualified observer controls
height; other viewers cannot change the shared geometry. A shrink that would
hide an active pane is refused.
Private family sessions reserve the viewer controls and geometry needed for safe
observation. They apply supported options only to their owned server and leave
unrelated sessions and the user's global tmux configuration untouched. A running
session retains its requested profile snapshot and reports effective settings.
Existing pane history capacity cannot be resized: changing `history_limit`
requires a new session. Other supported settings can be applied explicitly while
the owned session is idle, with native readback reported separately.

For scale, 20,000 fully filled 120-character lines contain 2,400,000 one-byte
character bytes. Adding 20% gives 2,880,000 bytes: 2.88 MB, about 2.75 MiB.
Four-byte characters give 11.52 MB with the same allowance. These are text
estimates, not bounds on tmux's native allocation; combining characters and its
grid structures need separate qualification. The per-pane retained-text budget
is 20,000,000 bytes. Native allocation is monitored separately rather than
presented as a stock tmux hard byte limit.

Automatic views return at most the newest 200 lines and 32 KiB of UTF-8 output
text in total, whichever limit is reached first. A single oversized line is
truncated safely; larger requested limits cannot expand the hard context budget.
Explicit pages of earlier retained output use the same bounds and report cursors,
gaps and truncation. Use `logs --before-cursor` to page backward and `--cursor`
to continue forward. `screen` reads the current pane rendering.
Durable command logs have their own rotation and retention, independent of tmux
scrollback. Missing tmux produces an explicit capability result and an optional
installation suggestion rather than automatic package installation.
`view` reads recent captured output. The output-text cap is 32,768 bytes and the
serialized CLI reply is capped at 65,536 bytes, including metadata. `--stream`
waits for one bounded final receipt and emits join reminders instead of repeating
raw output in the receipt. Session status lists the newest 20
operation summaries and reports truncation; individual operation handles retain
their own status and output. File-based stdin is limited to 8 MiB and an input
transaction to 1 MiB; oversized requests fail before dispatch. For larger local
files, run a command that reads the file directly.
Helper import paths stay internal: workloads retain the caller's `PYTHONPATH`
absence or value, with explicit command environment overrides applied afterward.

### Requested input and recovery

Viewers can scroll while application input is disabled. `input-lease` explicitly
requests bounded human input for one operation and grantee; leases expire using
a boot-bound monotonic deadline and can be revoked. Input is mediated through
the CLI with `--lease` and `--grantee`. Viewers become natively read-only during
delivery and regain scrolling only after pane input is verified disabled again.
Use a private `--data-file` when command-line text would expose sensitive input.

`doctor` reports state without repair; `doctor --repair` attempts only qualified
actions and records their outcomes. It can restore an exact idle view, label a
proven completed pane, or request one safe exit from a pager launched with
`run --controlled-app less`. Input receipts prevent replay after an uncertain
acknowledgement. Completion requires the operation and drain receipts, not just
successful keystroke delivery.

Fresh command panes do not reuse an interactive Bash prompt. An executable name
or prompt-looking text cannot prove an idle shell, clean Vim buffers or safe SSH
state. Unqualified applications, dirty editors, authentication prompts and unknown
jobs retain their state and return a bounded handoff with evidence and next steps.

### Runtime configuration

The existing profile file keeps its published format. Runtime-only preferences
use a companion JSON document under the same profile owner, with packaged
defaults in [runtime/agent-shell.json](../config/application-profiles/tmux/runtime/agent-shell.json)
and a [runtime schema](../config/application-profile-tmux-runtime.schema.json).
Missing local configuration is created atomically from
those defaults at startup. Valid values are loaded; invalid preference fields
produce warnings and use defaults only for those fields. Existing files and
valid siblings are preserved. Unsafe file ownership, unsupported required
features and unqualified native ownership stop application.

Each owned runtime establishes and verifies its complete effective configuration
before dispatch. It does not inherit the user's tmux configuration. The theme
uses the [branding palette](BRANDING.md#visual-contract-for-documentation-and-astrosite):
navy surfaces, electric-blue structure, readable technical text and labeled
states. One status row shows compact activity counters from actual operation
records and output activity. Join commands are advertised in progress messages
rather than occupying a separate footer row.

Legacy profile readers continue to use the unchanged profile file. New runtime
readers preserve bounded unknown optional extensions and ignore their behavior;
unknown required capabilities are rejected before native application. Older
releases cannot acquire new runtime features merely by reading a newer file.

### Completion and observers

The overview displays Idle; completed and failed command states remain distinct.
Once result recording, output draining and pane removal are confirmed, later
commands reuse that settled receipt rather than repeating its native cleanup
checks. New dispatch still verifies the current family inventory; unfinished or
uncertain cleanup continues through reconciliation. Concurrent lifecycle changes
can return `busy` before dispatch, which does not mean the workload ran.
Completed command panes are cleaned after result recording and output drainage.
Closing an idle family session prevents new dispatch while attached viewers keep
their view. The managed attachment lifecycle removes it after the last viewer
detaches, without requiring the agent to remain alive. Active or uncertain work
is preserved. Report `retiring` or cleanup pending until native removal is
verified; an attachment acknowledgement alone does not prove cleanup.

The collector handles idle sessions of exited owners after 15 quiet minutes;
live silent commands and attached human observers are preserved. Unknown panes
or unreadable operation records preserve the session for investigation.
Disconnect alone does not establish abandonment. Normal exit confirms actual
cleanup or reports pending resources; crash cleanup receipts are available on
the next startup or status check. A verified replacement after collection creates
a fresh session and returns `previous_cleanup`, identifying the removed session
and its cleanup time. Retained command logs remain available through old handles.

### Scheduled cleanup

`localsetup shell schedule install` enrolls the current user's fixed every-minute
collector. Supply `--grant-id`, `--owner-pid` and `--owner-starttime` explicitly.
The grant authorizes this cleanup recipe to continue after the
enrolling agent exits. `schedule status` checks it and `schedule disable` revokes
future runs and removes only its exact managed crontab block. These commands use
the same optional shell state root as command sessions.

Enrollment preserves unrelated crontab entries and creates a dated compressed
backup before changing the crontab. Readback resolves an uncertain write without
replaying it. Overlapping runs skip, and changed source or runtime dependencies
stop dispatch. A reboot requires an explicit same-host rebind; changes to the
recipe, source or authority require a new grant. No package installation, daemon
or privileged scheduler is added.

The initial profile fixes the collector interval at 60 seconds and keeps observer
protection enabled. Family startup reapplies the validated branded baseline. Other validated
settings can be saved as named preferences. A successful schedule check proves
the binding and crontab entry; a run receipt proves that a trigger actually ran.

## Optional terminal launcher

Enable it once per machine. Disable it any time with a single command. No manual file editing required.

---

## Why this exists

Without this mode, terminal sessions open as plain bash. An agent running host commands has no shared view with the operator, sudo prompts are invisible, and long-running commands are lost if the connection drops.

This mode fixes that in two layers:

- **Layer 1:** Automatically opens new terminals inside a tmux session (IDE profile or shell auto-attach, depending on mode).
- **Layer 2:** Injects a mandatory agent rule into your rules file so agents always use the tmux workflow and the sudo gate.

---

## Modes

Choose the mode that matches how you work on this machine:

| Mode | Use when | What it configures |
|---|---|---|
| `--mode ide` (default) | Using Cursor or VS Code Remote as your primary interface | IDE terminal profile (Layer 1a) + agent rule (Layer 2) |
| `--mode shell` | SSH sessions, servers without an IDE, CI, plain terminal | Shell RC auto-attach (Layer 1b) + agent rule (Layer 2) |

Both modes configure the same agent rule (Layer 2) and tmux_ops tool (Layer 3). The difference is only in how Layer 1 launches tmux.

---

## Quick start

```bash
# IDE mode (Cursor/VS Code Remote):
./ls/tools/tmux_terminal_mode enable

# Shell mode (SSH/plain terminal):
./ls/tools/tmux_terminal_mode enable --mode shell

# Check status:
./ls/tools/tmux_terminal_mode status

# Disable:
./ls/tools/tmux_terminal_mode disable
```

---

## Enable

```
./ls/tools/tmux_terminal_mode enable [--mode {ide,shell}] [--session NAME]
    [--settings-file PATH] [--shell-rc PATH] [--rules-file PATH] [--dry-run]
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--mode {ide,shell}` | `ide` | Which Layer 1 variant to apply |
| `--session NAME` | `ops` | tmux session name written into profile/RC/rule |
| `--settings-file PATH` | auto-detected | IDE settings.json path (ide mode only) |
| `--shell-rc PATH` | `~/.bashrc` (Linux) / `~/.bash_profile` (macOS) | Shell RC file (shell mode only) |
| `--rules-file PATH` | `.cursor/rules/operator-rules.mdc` | Agent rules file |
| `--dry-run` | off | Print what would change; modify nothing |

### What enable does

**IDE mode (Layer 1a):**

Auto-detects the settings file in this order:

1. `~/.cursor-server/data/Machine/settings.json` (Cursor Remote SSH, Linux)
2. `~/.vscode-server/data/Machine/settings.json` (VS Code Remote SSH, Linux)
3. `~/Library/Application Support/Cursor/User/settings.json` (Cursor Desktop, macOS)
4. `~/Library/Application Support/Code/User/settings.json` (VS Code Desktop, macOS)

Adds a `tmux-session` terminal profile and sets it as the default. The original settings file is backed up as `<file>.tmux-mode.bak`. After enable, restart the IDE terminal panel for the new profile to appear.

**Shell mode (Layer 1b):**

Appends a sentinel-wrapped block to your RC file:

```bash
# BEGIN tmux-default-terminal-mode
# Auto-attach to tmux session if not already inside tmux.
if [ -z "$TMUX" ] && [ -n "$PS1" ]; then
  exec tmux new-session -A -s ops
fi
# END tmux-default-terminal-mode
```

The `exec` replaces the shell process so there is no double-shell overhead. The guards ensure this only fires for interactive, non-tmux sessions.

> **Important:** Shell mode uses `exec tmux`, which means opening a new terminal is the only way to activate it, and `disable` + opening a new terminal is the only clean way back to a plain shell.

**Both modes (Layer 2):**

Appends a sentinel-wrapped mandatory tmux + sudo gate rule to the rules file. Agents following this rule will use `tmux_ops pick`, `tmux_ops probe`, and `tmux_ops run` for every host command.

### Idempotency

Running enable twice in either mode is safe. If the sentinel blocks are already present, they are not duplicated.

---

## Disable

```
./ls/tools/tmux_terminal_mode disable [--mode {ide,shell}]
    [--settings-file PATH] [--shell-rc PATH] [--rules-file PATH] [--dry-run]
```

Removes the Layer 1 variant for the specified mode and Layer 2 from the rules file.

- If a `.tmux-mode.bak` backup exists, it is restored and the backup is deleted.
- If no backup exists, only the sentinel block is removed; everything else in the file is untouched.
- If neither the sentinel nor a backup is found, disable exits 0 with "nothing to do."

After disabling IDE mode: restart the IDE terminal panel.
After disabling shell mode: open a new terminal to get a plain shell.

---

## Status

```
./ls/tools/tmux_terminal_mode status [--settings-file PATH]
    [--shell-rc PATH] [--rules-file PATH] [--json]
```

Reports all layers unconditionally regardless of which mode was used at enable time. Status is read-only: it does not create IDE settings files, shell RC files, rules files, or adapter entries. Example output:

```
tmux-default terminal mode status
  Mode detected:           ide
  Session name:            ops
  Layer 1a (IDE profile):  ACTIVE   [tmux-session -> ops, settings: ~/.cursor-server/...]
  Layer 1b (shell RC):     INACTIVE
  Layer 2  (agent rule):   ACTIVE   [rules: .cursor/rules/operator-rules.mdc]
  Layer 3  (tmux_ops):     PRESENT  [ls/tools/tmux_ops]
```

Partial or mixed states (e.g. Layer 1a active but Layer 2 missing) are reported as-is so you can diagnose and re-run enable for the missing layer.

Use `--json` for the stable machine-readable schema used by doctor and verify checks.

---

## Manual rollback

If the script is unavailable, revert manually:

**IDE mode (Layer 1a):**

```bash
# Option A: restore backup
cp ~/.cursor-server/data/Machine/settings.json.tmux-mode.bak \
   ~/.cursor-server/data/Machine/settings.json

# Option B: remove the two keys from settings.json with any editor:
# "terminal.integrated.profiles.linux" -> remove the "tmux-session" entry
# "terminal.integrated.defaultProfile.linux" -> remove the key
```

**Shell mode (Layer 1b):**

```bash
# Option A: restore backup
cp ~/.bashrc.tmux-mode.bak ~/.bashrc

# Option B: remove lines between (and including) these markers:
# BEGIN tmux-default-terminal-mode
# END tmux-default-terminal-mode
```

**Both modes (Layer 2 - agent rule):**

```bash
# Remove lines between (and including):
# BEGIN tmux-default-terminal-mode
# END tmux-default-terminal-mode
# from .cursor/rules/operator-rules.mdc (or whichever --rules-file you used)
```

All of these require only a text editor or `cp`.

---

## Changing the session name

The session name is written into the Layer 1 block and the Layer 2 rule at enable time. To change it:

```bash
./ls/tools/tmux_terminal_mode disable --mode <your-mode>
./ls/tools/tmux_terminal_mode enable --mode <your-mode> --session <new-name>
```

---

## Layer 3: tmux_ops

Layer 3 is the `ls/tools/tmux_ops` tool already present in the framework. Enable does not modify it. The agent rule (Layer 2) references it for pick, probe, run, status, and cancel operations. Status reports whether the tool is present.

For full tmux_ops documentation see the `ls-wf-sudo-tmux-session` workflow package, [ops/tmux-ops-managed.md](ops/tmux-ops-managed.md), and [ops/tmux-ops-remote.md](ops/tmux-ops-remote.md).
