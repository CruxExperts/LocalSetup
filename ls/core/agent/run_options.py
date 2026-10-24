"""Public run arguments, without importing the SDK or initializing a provider."""
from pathlib import Path


def arguments(parser):
    parser.add_argument('--profile', required=True)
    parser.add_argument('--profiles', type=Path)
    parser.add_argument('--grant', type=Path, required=True, help='Explicit private read/write/disclosure and recipe JSON')
    input_mode=parser.add_mutually_exclusive_group(required=True)
    input_mode.add_argument('--prompt-stdin', action='store_true')
    input_mode.add_argument('--interactive', action='store_true', help='Terminal multiline input, steering and per-tool approvals')
    parser.add_argument('--format', choices=('text','jsonl'), default='text')
    parser.add_argument('--retain-events', action='store_true', help='Retain bounded filtered views; requires signed output_filters in the grant')
    parser.add_argument('--event-context', metavar='RUN:BUFFER_CHECKPOINT',
                        help='Disclose a selected retained page on resume; requires signed output_filters')
    parser.add_argument('--event-after', type=int, default=0, help='Retained-event cursor (default: 0)')
    parser.add_argument('--event-limit', type=int, default=20, help='Retained events to include, 1..64 (default: 20)')
    parser.add_argument('--event-max-bytes', type=int, default=16384, help='Retained context byte limit, 1024..65536')
    parser.add_argument('--image', action='append', default=[], help='Explicit workspace-relative PNG/JPEG attachment (repeatable)')
    parser.add_argument('--context', action='append', default=[], help='Explicit workspace-relative context file (repeatable)')
    parser.add_argument('--skill', action='append', default=[], help='Explicit workspace-relative SKILL.md file (repeatable)')
    parser.add_argument('--workspace', type=Path, default=Path.cwd())
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--resource-parent', type=Path, required=True)
    parser.add_argument('--approve-tools', action='store_true', help='Require one-use owner approval for each tool request (control FD and JSONL required)')
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument('--control-fd', type=int, help='Inherited local stream socket for schema-1 status/cancel messages')
    controls.add_argument('--encrypted-control-fd', type=int,
                          help='Inherited relay socket for encrypted control; requires remote_control grant and JSONL output')
    parser.add_argument('--control-nonce', help='Fresh 32-hex controller challenge; required with --encrypted-control-fd')
    parser.add_argument('--task')
    parser.add_argument('--session')
    history=parser.add_mutually_exclusive_group()
    history.add_argument('--require-new-session', action='store_true', help='Atomically require an absent session; excludes explicit context, skills and images')
    history.add_argument('--resume',metavar='CHECKPOINT')
    history.add_argument('--recover-from',metavar='CHECKPOINT')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--request-limit', type=int, default=8)
    parser.add_argument('--tool-limit', type=int, default=16)
    parser.add_argument('--token-limit', type=int, default=32768)


def defaults(args):
    from .diagnostics import locations
    found = locations(Path.home())
    args.runtime_root = (args.runtime_root or Path(found['runtimes'])).absolute()
    args.state_root = (args.state_root or Path(found['state'])).absolute()
    args.profiles = (args.profiles or Path(found['profiles'])).absolute()
    args.grant = args.grant.absolute()
    args.workspace = args.workspace.absolute()
    args.resource_parent = args.resource_parent.absolute()
    return args
