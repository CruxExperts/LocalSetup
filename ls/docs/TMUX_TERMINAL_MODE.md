---
status: ACTIVE
version: 4.89
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
120-column initial width until a qualified primary viewer supplies its terminal size.
Layout changes follow changes to the visible panes;
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

Backend availability and owner readiness are separate checks. `shell capabilities`
qualifies the tmux backend; it does not establish that the calling tool runner can
act for a native agent. The supported integration launches the CLI within the live
process ancestry of the verified, long-lived agent owner. Pass that owner's PID
and Linux process start time together using `--owner-pid` and `--owner-starttime`,
or `LOCALSETUP_SHELL_OWNER_PID` and `LOCALSETUP_SHELL_OWNER_STARTTIME`. The CLI
rechecks ancestry and process generation; supplying these values does not grant
authority to a caller outside that ancestry. `--agent-id` identifies an already
qualified owner and does not replace the process checks.

A detached or brokered tool runner outside the native owner's process tree is
currently unsupported for managed shell mutations, including `ensure`. An
available backend can therefore still return `owner_identity_required` when the
binding is absent, `owner_not_ancestor` when the live owner is outside the caller's
ancestry, or `owner_generation_mismatch` when its process start time differs.
Configure the client to launch its command runner beneath the actual native
owner when that integration is available. Otherwise report managed-shell
execution as unavailable for that runner and use the client's ordinary command
tool within the existing authorization and sandbox. Preserve existing sessions;
an observation handle is not mutation authority. Do not substitute a short-lived
helper, an unrelated same-user process, a fabricated binding, or a replacement
session to get past the owner check. There is no detached-runner trust bypass or
enrollment flag in this interface.

New status panes have a one-second observation window to finish starting their
expected process. This wait only reads the original pane and process identity;
it never recreates the pane or repeats a command. A changed PID or process start
time, a dead pane, or a persistent command mismatch fails closed. Temporary
process cleanup still requires the qualified identity and a process descriptor.

Agents announce whether they created or joined the session, identify its machine,
and show its local join command at startup, command start and at least once every
five minutes during long-running work. Keep it discoverable in the owned session
progress messages so a returning observer can find it without searching old messages.
The user runs that command on the named machine; it contains no SSH assumption.
The CLI prints a deterministic join banner to standard error after `ensure`,
`join`, and command dispatch, before a streamed command starts waiting. The
banner centers the verified host between hash borders, preserves the complete
service-generated attach command, and uses one shared width for every entry.
When a session uses an explicit `shell --state-root /absolute/private/path`, its
attach, operation-log, input and family-membership instructions retain that directory,
including the attach row in older two-row status layouts.
Copy the complete command, including its shell quoting for spaces or apostrophes;
omitting the directory searches the default store and cannot resolve that session.
Sessions using the default store keep their existing shorter commands. Banner
validation accepts only canonical, safely quoted attach instructions and an
optional absolute state directory; it rejects extra options and shell syntax.
Redirected output is plain text; color is limited to a capable terminal and is
disabled by `NO_COLOR` or `TERM=dumb`. Set `FORCE_COLOR=1` when a terminal-capable
agent captures output through a non-interactive stream; `NO_COLOR` still takes
precedence. Blank lines isolate the banner from neighboring output, and its
host/border lines use cyan independently of the pane theme. JSON results remain
on standard output.
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
the idle view until its completed-section timeout or an accepted replacement.
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
`root · tests` or `sub scout · source-map`. Use meaningful operation IDs;
command arguments, paths and application-provided titles are not identity labels.
The divider also shows the operation's verified launch directory. It uses the
full path when space allows and a middle `...` otherwise, preserving a useful
prefix and final folder name. Presentation sanitizes terminal controls and tmux
format syntax without changing the stored operation identity or path. The launch
directory is not a claim of live tracking after a script changes directories.
Native headers use the selected theme's accent for identity, text for purpose,
and muted color for location; state has its own semantic color. Abbreviating
the display to `sub` does not change membership or authorization.
Existing legacy sessions keep their layout and handles. `--layout legacy`
remains available for that compatibility path; requesting a different layout
does not silently convert an existing session.

### Native client activity

For a qualified local Codex daemon owner, `shell ensure` uses `CODEX_THREAD_ID`
to start one managed observer for that native client session. Its activity pane
shows supported root and descendant-agent tools, shell work, edits, web searches,
images and collaboration calls. It retains only item identity, kind, tool name
and state; messages, reasoning, commands, arguments, results and descriptions are
discarded. Admitted shell panes still provide their separately authorized output.
Use `ensure --no-client-activity` to skip automatic startup for that invocation.

```bash
localsetup shell observe --handle H --thread THREAD
localsetup shell activity --handle H
localsetup shell observe --handle H --thread THREAD --restart
```

These commands require the bound session owner. The adapter qualifies the native
daemon process generation, Unix socket peer and root workspace. Its RPC allowlist
contains only initialization and reads: it never resumes a thread, starts a turn,
executes tools or answers approvals. Native retained-item snapshots provide the
data; this is not a complete action stream or transcript reader. The current
adapter discovers at most 32 descendants, starts with 32 retained items per thread,
and advances at most four pages per refresh. In-progress items are checked for
completion. The metadata cache holds 2048 items, and `activity` exposes the latest
128. `history_gap` reports omitted history or bounded coverage. Native methods
that are unavailable produce an unavailable result. See the upstream
[app-server protocol](https://learn.chatgpt.com/docs/app-server).

The observer is an ordinary managed operation: repeated enrollment reuses its
exact operation, and explicit restart requires the previous process to be
collected. Closing the session stops the observer. Verified owner exit, a non-ready
session, changing its owner or disabling the preference ends its loop. Transient
read or transport errors reconnect within the same managed operation, requalifying
the native peer and workspace. The bounded retry budget defaults to three attempts
with 1, 2 and 4 second delays; pending item references and the deduplication cache
survive. Snapshot cursor and pending-state changes commit only on a complete
successful read, so a later transport error cannot lose an earlier completion.
Discovery revalidates pending threads, and `history_gap` remains set after
recovery. Permission and identity/validation failures are fatal and never retried.
Starting, live and reconnecting snapshots become stale after their reported
freshness bound. Exhausted recovery is unavailable; ordinary command panes stay
usable. Re-enable a stopped observer
with an explicit restart; uncertain operations are reconciled rather than replayed.

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
| `stable` (startup default) | Divide visible space equally; output alone never changes geometry. |
| `follow` | Reveal newly active work; deliberate reading temporarily protects the view. |
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

Fresh native sessions keep jobs in order, with consecutive collapsed jobs rendered
as individual title rows between expanded terminals. Hidden applications retain
their native terminals and parked dimensions. The page budget includes titles,
tmux separators, minimum application heights and controls. Older live sessions
keep their recorded representation; refreshing controls does not migrate them.

Click a title or terminal to inspect it. Expansion uses the space below that title,
leaving panes above it in place and collapsing lower jobs into title rows. A bottom
pane with no space below stays in place; use scrollback or Logs to read more.
Inspection lasts for 15 seconds after deliberate interaction. Scrolling and
keyboard navigation renew it; background output does not. Copy mode and dialogs
protect reading. When inspection ends, the previous mode, page, selection and
proportions return, with newly added jobs accounted for.

The fixed top row identifies LocalSetup, the actual tmux session ID and name, and
the repository or working location. Location is shortened before session identity.
The bottom row contains Stable, Follow, Fixed, page navigation, Panes, Logs, Menu
and Exit. Selection marks and text labels accompany color. Controls repaint even
when the selected mode is already active. Geometry changes coalesce for 100 ms;
manual controls do not wait for command output or Follow's automatic delay.

Menu (F6) groups Appearance, Reading and layout, Status detail, Session behavior,
and Keyboard help. Settings show their current values and purpose. Use mouse
selection, Tab or Shift-Tab to move focus, arrows to navigate, Enter to apply and
Escape to return. Pane choices identify their purpose and state. Titles distinguish
Idle, Running, Input needed, Completed, Failed and Finishing in pane headers.
Native exit notices such as “pane is dead” are suppressed; status is not added to
application output. Details carries timing,
byte counts and diagnostic identifiers.

Native menus, read views, theme previews and forms are centered horizontally and
vertically. Their upper-right `[ESC]` target is clickable and uses the same
dismissal or cancellation path as Escape. The selected theme controls their
interior and border colors; text and focus cues remain readable without color.

Escape first dismisses a dialog, cancels a drag, leaves copy mode, or restores an
inspection. Such an Escape does not arm exit. At the main view, two Escapes within
500 ms detach only that viewer. Exit and Ctrl-Q provide alternatives without a
timing requirement. Secondary viewers can leave without acquiring layout authority.

Midnight is the dark default theme; Graphite and High Contrast are also available.
Theme previews stay inside the chooser. Enter or Apply accepts a theme; Escape
cancels the preview. Control colors never rewrite application ANSI formatting or
captured output. JSON and redirected command output do not gain presentation escapes.

Runtime JSON configures startup mode, inspection duration, redraw debounce,
double-Escape timing, automatic closure, status detail, toolbar visibility and
colors. The `native_shortcuts` mapping configures menu and navigation keys for
new sessions; Escape, Ctrl-Q and mouse routes remain protected. Changing those
bindings requires a new session. Fresh grouped sessions show at most eight jobs
per page; a larger `max_sections` value still paginates at that effective limit.

The shared terminal runtime profile also defines:

- `completed_hide_seconds`: 60 by default, configurable from 1 to 86400 seconds.
  The Session menu offers 30, 60, 120 and 300 seconds.
- `client_activity_enabled`: true by default for qualified native clients.
- `client_activity_interval_seconds`: 1 by default, from 1 to 60 seconds. This is
  refresh pacing, not a delivery-latency guarantee.
- `client_activity_reconnect_attempts`: 3 by default, from 0 to 10. Zero disables
  automatic recovery. A successful complete snapshot resets the consecutive budget.
- `client_activity_reconnect_delay_seconds`: 1 by default, from 1 to 30 seconds.
  Consecutive delays double up to a 30-second ceiling; owner and session state are
  checked again before every connection attempt.

Completed and failed command sections hide after the configured idle interval
following completion or latest output, once process collection and output draining
have settled. Explicit selection or unfolding pins a section; restoring it releases
the pin. Inspection and required input also protect it. Automatic selection does
not pin a pane. Hiding changes rendering only: receipts and bounded logs remain
available through their handles. It does not expire individual rows in the ongoing
client-activity pane or close the live owner's session. This timeout is separate
from the session-retirement delay below.

Built-in versioned JSON themes live in `ls/config/application-profiles/tmux/themes/`.
Custom definitions use the same name, description and semantic-role format.
Older flat custom palettes remain supported; the previous stock palette resolves
to Midnight. Make default uses the bound signed configuration transaction when
writable authority is available to the qualified primary viewer. Without that
authority the change remains session-only and the control explains why. Conflicts
and uncertain native effects retain the last accepted configuration until readback
reconciles the original request.

After explicit owner closure or verified owner exit, the session waits for work
and output to drain before showing Session finished. Each viewer detaches after
30 seconds without interaction by default; activity and protected reading postpone
closure. The owned session is removed after the last viewer leaves. Unknown work,
ownership, or cleanup results remain pending. A completed command alone never
closes a session whose owner is still active.

Before the first command, the workspace explains its controls and layout modes.
Divider adjustments enter Fixed. Qualified overlays protect the layout, and an
orphaned owned overlay must be dismissed before deferred changes resume.

After updating the UI source, the owning controller can run
`localsetup shell refresh-ui --handle H`. It replaces only the verified control
and summary renderers, preserving the session handle, attached viewers and
workload panes. Active overlays, input handoffs and unsettled layouts defer the
refresh. Replacement process identities are checked after dispatch; an uncertain
result is retained for diagnosis and is never blindly replayed.

Existing sessions retain their runtime palette until the owner rejoins with
`localsetup shell ensure`. When applying a theme update, preserve the session's
non-theme settings and wait for overlays, copy mode and input handoffs to finish.
Then use `refresh-ui` to repaint the controls and collapsed rows. Theme refresh
does not require detaching the viewer or restarting its workloads.

Follow activity includes launch, output resuming after dormancy and a requested
input lease. Events coalesce for 100 ms, automatic view changes are at least two
seconds apart, and collapse waits for ten quiet seconds. The selected operation
stays expanded. Copy mode, overlays and mediated input defer rearrangement;
status reports pending layout and background activity. A protected application
keeps its geometry when the viewer becomes smaller, showing a clipped view until
the interaction ends.

The primary viewer alone controls the shared layout and dimensions. The native
canvas uses the 120-column unattached default, accepts widths from 80 to 500
columns and debounces resize events for 100 ms. Layout state reports desired and
applied generations. An uncertain mutation stops further layout writes until
native identities and geometry are reconciled; it is never blindly replayed.
Large layouts use one streamed tmux command queue so accumulated panes do not
exceed the command-message size limit. An explicit `doctor --repair` can build
a fresh layout generation from a fully qualified pane inventory after a failed
placement. It preserves workload identities and never resubmits a command;
unknown occupants, uncertain workloads and protected input prevent that repair.
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
cannot select or resize it. In the native canvas, when the primary departs, the
oldest remaining qualified attachment becomes primary and the canvas fills that
viewer’s terminal. Unknown or conflicting observer identities prevent handoff.
The earlier rendered canvas retains its original first-attachment behavior.
Join commands remain in operation replies and
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
their own status and output. Native layout and retention detail collections are
also limited to 20 entries in CLI replies, with counts and truncation flags.
Individual operation replies select that operation's retention detail instead
of repeating the whole family's history. Aggregate retention uncertainty,
layout state, page counts and generation numbers remain visible. These output
bounds do not change the stored layout, history or running processes.
File-based stdin is limited to 8 MiB and an input
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

Static descriptions (up to 32 KiB), field labels and confirmation phrases wrap to the available
terminal width. PageUp and PageDown scroll the form details without editing a
field; typing or changing focus returns the view to the active input. Resizing
keeps the current input and clamps the view. Password masks remain fixed while
browsing. The first two title rows stay visible above the scrolling content;
longer titles continue within the details so the controls always remain available.

Color-capable dialog helpers use the session's selected semantic theme for text,
filled header and field backgrounds, selections, and errors.
The renderer maps those colors to existing terminal colors without redefining
the terminal's palette. Labels and focus indicators remain available without color.

A password field can name an earlier password field with `matches`. Matching is
checked in memory before submission; differing entries stay editable and produce
a generic error without displaying either value. Operation consumers still
validate the received pair independently before using it.

The library runs in a private helper TTY, disables echo and terminal flow control,
and restores the original terminal settings on exit. A response is transferred
once in memory; cancellation and expiry clear mutable buffers. Python and terminal
libraries may retain temporary copies, so this is not a memory-zeroization
guarantee. The consumer must dispose responses after use.

Trusted operation owners can present these forms through
`ShellCommandService.dialog`. The controller supplies its operation identifier,
form, monotonic deadline, current-operation validator and synchronous delivery
callback. The service requires the exact live session owner and one qualified
primary viewer. It binds the request to that viewer's process generation and
terminal, the machine/session/endpoint, and the existing overlay reservation.
An existing popup makes a competing request return busy; it is never replaced.
Rearrangement remains deferred through the shared overlay lifecycle.

The fixed renderer receives its form over a mode-0600 Unix socket in a transient
mode-0700 directory. Arguments contain channel identifiers only. Admission checks
local peer credentials, exact helper arguments/executable and native-server
ancestry; a kernel process descriptor preserves the admitted helper generation.
The helper verifies its controller before receiving the form and disables core
dumps and process dumpability before accepting input. Responses use bounded
binary frames and are validated again by the consumer side. The dialog transport
keeps them out of ordinary shell input records, hashes, command arguments,
environment variables and logs; consuming workflows must preserve that boundary.
These checks prevent stale or unrelated local processes from receiving input;
they do not establish isolation from a compromised user account.

The delivery callback must revalidate its operation and complete its one bounded
handoff before returning exactly `True`. It must not retain the response for
asynchronous use. Exceptions or a missing consumer acknowledgement produce
`delivery_unknown`; the response is never replayed. A known consumer acceptance
remains delivered even if the renderer loses its acknowledgement. Public results
contain identifiers, delivery state and cleanup state, never field values.
Controller/client loss, cancellation, expiry and failed admission preserve those
boundaries. An uncertain popup close remains cleanup pending.

An operation owner may copy validated input into one bounded, private, one-use
handoff slot during that callback. It must not retain the `Response` or its
buffers. After the dialog returns, it must require a delivered result and
revalidate its exact operation, ownership and input deadline before consuming
the copy. Every failed handoff clears the slot. Long cryptographic operations
and durable trust writes run after the callback releases the session lock.

This is an internal service API, not a `localsetup shell` dialog command. The
[managed sudo adapter](ops/tmux-ops-managed.md#private-password-dialog) connects
an explicitly owned family session to the existing operation-bound askpass
channel. Its password popup uses the same sudo invocation and authentication
deadline after ordinary generic input is exhausted. The
[OpenPGP native-input adapter](OPENPGP_RUNTIME.md#native-passphrase-and-certificate-confirmation)
supports protected key generation, byte/file-envelope signing/opening, typed
certificate enrollment and create-once persistent authority initialization. A form
submission alone does not authorize an operation or prove authentication succeeded.

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
uses the [branding palette](BRANDING.md#current-visual-assets):
navy surfaces, electric-blue structure, readable technical text and labeled
states. One status row shows compact activity counters from actual operation
records and output activity. Join commands are advertised in progress messages
rather than occupying a separate footer row.

Legacy profile readers continue to use the unchanged profile file. New runtime
readers preserve bounded unknown optional extensions and ignore their behavior;
unknown required capabilities are rejected before native application. Older
releases cannot acquire new runtime features merely by reading a newer file.

For signed live tuning, register an owner with `{"kind":"terminal-runtime"}`
in the private [configuration authority binding](COMMAND_REFERENCE.md#signed-configuration).
Export and flush its complete settings through `localsetup config`, then bind
that owner to the qualified native canvas:

```bash
localsetup shell configuration --handle "$session_handle" \
  --authority-file "$authority_file" --config-owner terminal \
  --owner-pid "$agent_pid" --owner-starttime "$agent_start" --agent-id "$agent_id"
```

The existing renderer consumes verified revisions through the shared cadence;
no second background job is created. Eligible settings include minimum height
for newly launched operations, activity and resize timing, and qualified
presentation values. Native quiet time is 1–300 seconds, automatic view delay
1–60 seconds, coalescing and resize delay 50–1,000 milliseconds, and ratio steps
1–50 percent. Defaults remain unchanged. The approved palette remains
schema-enforced. Native borders receive verified readback, controls repaint, and
layout recalculates without replacing workload processes. Existing operations
retain their explicitly assigned minimum height. The renderer does not sign or
flush another process's staged memory.

The exact authority-file bytes and registered owner remain pinned for the session.
Key bindings, recovery actions, capacity, geometry defaults and security settings
cannot be changed through this live-tuning path. An invalid or conflicting signed
revision retains the last applied settings. A lost native acknowledgement leaves
an explicit pending receipt; `shell configuration --handle "$session_handle"
--reconcile` with the same owner flags checks the exact signed revision and native
readback without repeating writes. A later signed revision does not prevent
reconciliation of the exact pending ancestor; the next cadence can apply that
later revision. Mismatched or partial readback remains pending
for controller diagnosis. Applied receipts contain revision, digest and redacted
signed provenance; the latest rejection is retained in private session state.

The same owner-authorized reconciliation command also reads back an interrupted
session-only theme or presentation change, including sessions without a signed
configuration binding. It accepts only the retained pending settings and never
replays the native write. Matching readback clears the pending receipt; a mismatch
preserves it for diagnosis. CLI output's 80-column baseline does not resize this
TUI canvas or restrict a wider attached terminal.
Shell status reports the applied revision, digest, pending state and latest
error without exposing the private authority binding.

### Completion and observers

The overview displays Idle; completed and failed command states remain distinct.
Once result recording, output draining and pane removal are confirmed, later
commands reuse that settled receipt rather than repeating its native cleanup
checks. New dispatch still verifies the current family inventory; unfinished or
uncertain cleanup continues through reconciliation. Concurrent lifecycle changes
can return `busy` before dispatch, which does not mean the workload ran.
After result recording, output drainage and process collection, retain at most
one verified completed result pane for read-only viewing. Its cleanup state is
`retained`, rather than falsely claiming native removal. Its header and output
follow the configured completed-section visibility timeout and explicit unfold
pins; a failed or busy command admission must not clear it. An accepted replacement
or verified family teardown removes the owned
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

While a command runs, its supervisor also collects exited children orphaned by
the workload. Both native and canvas runners check twice per second, preserving
the main command's exit status and leaving live descendants running. This keeps
long test runs from accumulating zombie processes until final cleanup. Zombies
consume process slots; they do not explain large resident-memory growth.

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

### Keyboard and contrast design references

Controls follow the [W3C keyboard guidance](https://www.w3.org/WAI/WCAG22/Understanding/keyboard.html):
pointer actions have keyboard equivalents, including an exit without timed keystrokes.
Built-in theme text pairs meet the [4.5:1 minimum contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html).
These are control-design references; they do not certify third-party terminal
applications or user-customized palettes.
