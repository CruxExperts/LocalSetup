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
own observation sessions. Concurrent panes stack with horizontal dividers at a
120-column default width. Layout changes follow changes to the visible panes;
repeated observation should preserve their geometry. Stable handles resolve
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
The CLI prints a deterministic join banner to standard error after `ensure`,
`join`, and command dispatch, before a streamed command starts waiting. The
banner centers the verified host between hash borders, preserves the complete
service-generated attach command, and uses one shared width for every entry.
Redirected output is plain text; color is limited to a capable terminal and is
disabled by `NO_COLOR` or `TERM=dumb`. JSON results remain on standard output.
For a read-only batch, repeat `--handle` in the desired order:

```text
localsetup shell advertise --handle H1 --handle H2
```

Each handle is inspected through the existing service and native session
verification. Invalid or unready targets receive per-entry JSON errors, valid
targets still appear in the banner, and the command exits nonzero if any target
failed. Advertisements contain only service-generated `shell attach` commands;
they never include invitation, admission, or join tokens.
Completed operations remain inspectable through retained status and logs after
their command panes have been removed. One collected result pane may remain as
the idle view so the last output stays readable until an accepted replacement.
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

New sessions default to the native `canvas-v3` presentation. Use
`ensure --layout family` to explicitly select the earlier family layout. The root uses `shell admit`
with `--member-agent-id` and a short `--label`, then passes the returned one-time
invitation privately to that subagent. The subagent calls `shell join --handle`
with `--invite` and its own live owner binding. Membership does not authorize
sibling operations or family administration. The root remains responsible for
the shared session and topology.

Pane labels use the admitted identity and operation ID as a purpose slug:
`root · tests` or `subagent scout · source-map`. Use meaningful operation IDs;
command arguments, paths and application-provided titles are not identity labels.
The divider also shows the operation's verified launch directory. It uses the
full path when space allows and a middle `...` otherwise, preserving a useful
prefix and final folder name. Presentation sanitizes terminal controls and tmux
format syntax without changing the stored operation identity or path. The launch
directory is not a claim of live tracking after a script changes directories.
Existing legacy sessions keep their layout and handles. `--layout legacy`
remains available for that compatibility path; requesting a different layout
does not silently convert an existing session.

### Native activity canvas

`ensure --layout canvas-v3` (also `--layout native`) explicitly selects the default
native presentation. It keeps each operation in a persistent native tmux
pane. tmux renders application colors, cursor movement, progress displays and
full-screen applications directly. Hidden operations keep running in owned
parking windows; their pane and process identities do not change. A layout
change prepares destination geometry before restoring a pane. Ordinary output
does not rebuild the screen or reorder operations.

Use `localsetup shell layout --handle H` to inspect a native session. Its modes
are mutually exclusive and persist with the session:

| Mode | Behavior |
| --- | --- |
| `follow` | Reveal newly active work while preserving operation order. |
| `stable` | Keep the selected operations and focus; show background activity separately. |
| `fixed` | Keep the selection and saved proportions, including across resizes. |

```bash
localsetup shell layout --handle H --mode stable
localsetup shell layout --handle H --select build
localsetup shell layout --handle H --ratio build=2 --ratio tests=1
localsetup shell layout --handle H --mode follow
```

Manual operation or page selection enters stable mode. Ratio edits enter fixed
mode; returning to fixed restores its saved weights. Weights are independent of
rounded screen rows. Newly added operations receive an equal initial share while
existing weights retain their relative proportions. Insufficient space exposes
overflow through pages rather than reducing an application to a one-row terminal.
The saved selection and proportions remain available when space returns.
The shared `expanded_min_rows` preference defaults to ten rows including the
title, and `shell run --expanded-min-rows` overrides it for one operation. The
native application keeps at least three content rows even with a smaller override.

New native sessions show dormant jobs as one-line entries in a shared strip above
the expanded applications. The strip has one shared heading; its rows participate
in the same page budget as application titles, minimum heights and bottom controls.
`max_sections` limits the total jobs per page (eight by default). Clicking a compact
entry or selecting it through F6 opens that job. A resumed job moves beneath older
active jobs; continuous output does not repeatedly reorder the stack. The most
recently active job stays open after it becomes quiet. Page arrows and the page
counter remain at the bottom, and manual browsing preserves its page anchor.
Commands that finish before a follow transition retain their native output until
that transition completes. Cleanup also preserves explicitly selected off-page
applications, so returning to their page does not lose the chosen view.

The bottom controls show the current mode before optional telemetry. ASCII
terminals use `(*)` and `( )` radio marks. F6 opens the native menu; arrow keys,
Enter and Escape provide keyboard selection and dismissal. The pane selector,
layout editor, bounded logs and help return to the existing application view.
Before the first command, the workspace shows the keyboard and mouse controls
and explains the three layout modes. This welcome view resizes with the terminal;
it gives way to application panes as work starts.
Divider adjustments enter fixed mode. Overlays are short-lived: their exact
client lifetime protects the layout, and an orphaned owned overlay is dismissed
before deferred changes resume.

Follow activity includes launch, output resuming after dormancy and a requested
input lease. Events coalesce for 250 ms, automatic view changes are at least two
seconds apart, and collapse waits for ten quiet seconds. The selected operation
stays expanded. Copy mode, overlays and mediated input defer rearrangement;
status reports pending layout and background activity. A protected application
keeps its geometry when the viewer becomes smaller, showing a clipped view until
the interaction ends.

The primary viewer alone controls the shared layout and dimensions. The native
canvas uses the 120-column unattached default, accepts widths from 80 to 500
columns and debounces resize events for 250 ms. Layout state reports desired and
applied generations. An uncertain mutation stops further layout writes until
native identities and geometry are reconciled; it is never blindly replayed.
Existing sessions retain their recorded presentation and explicit legacy choices
remain available. Attachment does not grant application input authority: use
the existing `input` and `input-lease` APIs.

Native history and durable rolling logs are separate. Terminal reflow, configured
history capacity and application-controlled alternate screens affect what native
scrollback retains. Inspect the reported retention limits and use bounded log
pages for captured output; rotation gaps and capture failures remain explicit.
Layout preservation is not a promise of unlimited history or of an application's
own redraw behavior.

### Rendered activity canvas compatibility

`ensure --layout canvas` selects the `canvas-v2` runtime for a new session.
Existing `family-v1` and legacy sessions keep their native panes and are never
migrated while running. The canvas keeps one tmux renderer pane. The existing
command runner owns each workload's private child PTY, process identity,
bounded capture and completion receipt. A quiet section changes presentation
only; its process keeps running. Each dormant header occupies one physical row
above expanded sections. Launch, output after dormancy and a new input request
advance the activation order; continuing output does not reorder sections.
The most recently activated section stays expanded after all output quiets.

Expanded sections have a 10-row default minimum including their title; an
operation may use `run --expanded-min-rows <rows>` for a 2–100 row minimum.
The private runtime profile sets the quiet period (default 5 seconds), maximum
sections per page (default 8), activity coalescing (250 ms), and trailing
resize debounce (250 ms). Sections are packed into logical pages without
splitting; spare rows are divided among expanded sections in proportion to
their minima, with stable displayed-order ties. The bottom row always shows
Page left and Page right, marking an unavailable edge. Canvas sessions turn off
tmux's separate status bar so this remains the one visible bottom control row.
The qualified primary viewer uses Left, Right, or a click on either footer
label; a manual selection anchors its operation until the viewer returns to
the newest activation page. Secondary viewers mirror that shared page and
cannot select or resize it. If the primary departs while secondaries remain,
none takes over automatically; after all viewers detach, the next qualified
attachment becomes primary. Join commands remain in operation replies and
progress messages. No extra tmux windows or workload suspension implement
pagination.

The primary viewer's actual width can resize the canvas from 80 to 500
columns, and its height from 1 to 512 rows. Width below 80 and insufficient
height show a resize notice while work continues. One physical resize applies
after the border settles for 250 ms. Canvas output is bounded text: control
characters and terminal escape bytes are displayed literally, not replayed.
Headers are recalculated for the current width, retaining the final working
directory component when a long path needs a middle ellipsis. Explicit command
stdin uses a bounded pipe; mediated interactive input uses the owned PTY.
If log storage fails, the runner keeps supervising and draining the workload,
then reports output unavailable without claiming a captured result.
Full-screen terminal applications and the controlled `less` mode have no
qualified canvas presentation; use `family-v1` for those workflows. `screen`
on a canvas operation returns its captured text; session `screen` shows the
shared rendered page. Each operation's rolling capture remains capped at
20,000,000 bytes, with explicit cursor gaps on rotation. The renderer's
native history cap is calculated for the widest supported viewport.

To replace a closed legacy runtime while its viewer remains, explicitly close
that idle handle and use `ensure --replace-retiring` with the same root identity.
The new family receives a new handle. Old viewers keep their retired view and
old operation logs remain available; their eventual detach cannot remove the
new runtime. Only one generation accepts new work.

Application profiles are schema-validated JSON with shipped defaults and private
saved preferences. `localsetup app-profile --help` exposes resolution and origin
reporting. The tmux profile keeps the bottom session/window bar visible for
legacy and `family-v1` sessions; the canvas uses its single footer instead.
The profile enables the mouse and requests 20,000 history lines. Family sessions report the
effective native capacity separately: the byte budget can reduce that line count.
The default qualified family capacity is 6,000 lines at 120 columns. Native
`family-v1` accepts widths from 80 to 160 columns and derives a safe history ceiling
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
raw output in the receipt. A completed operation returns its receipt immediately,
without replaying older retained logs; the timeout limits waiting for active work.
Session status lists the newest 20
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
`--data-file` avoids putting input in command arguments, but the ordinary input
API retains request data and digests. It is not a password handoff channel.

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

### Reusable terminal forms

The internal `terminal_ops.dialog_model` and `dialog_terminal` library supplies
password, single-line text, multiline text, checkbox groups, radio groups and
typed-confirmation fields. It has no shell execution or persistence path.
Requests use a bounded declarative schema; executable validators are rejected.
Forms allow up to 32 fields, 64 KiB of editable input in total, and 256 choices.
Each editable field defaults to 4096 bytes. Passwords and typed confirmations
cannot have initial values. Password display is a fixed mask that does not
reveal the value's length.

Tab and Shift-Tab move focus; arrows move the caret or choice highlight; Space
changes a choice. Enter activates the focused button, moves to the next
single-line field, or inserts a newline in multiline text. Ctrl-S submits;
Escape or Ctrl-C cancels. Bracketed paste is treated as literal data, so pasted
keystrokes cannot submit or cancel a form. Invalid or oversized input is rejected
without echoing it in an error. The renderer reads actual terminal dimensions,
preserves input and focus on resize, and disables submission below 40 columns
or 10 rows. Expiry and cancellation remain available at smaller sizes.

The library runs in a private helper TTY, disables echo and terminal flow control,
and restores the original terminal settings on exit. A response is transferred
once in memory; cancellation and expiry clear mutable buffers. Python and terminal
libraries may retain temporary copies, so this is not a memory-zeroization
guarantee. The consumer must dispose responses after use.

The forms are an internal component, not yet a `localsetup shell` dialog command.
Native client admission, the private response channel, and sudo, OpenPGP and
enrollment adapters are separate integration work. A form submission alone
does not authorize an operation or prove authentication succeeded.

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
After result recording, output drainage and process collection, retain at most
one verified completed result pane for read-only viewing. Its cleanup state is
`retained`, rather than falsely claiming native removal. Keep its header and
output in place during observation; a failed or busy command admission must not
clear it. An accepted replacement or verified family teardown removes the owned
retained view. A quiet running process remains active and cannot be reclaimed as
an idle result.
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
