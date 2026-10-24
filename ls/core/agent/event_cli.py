"""Read retained event pages without provider access or execution authority."""
import json
from pathlib import Path
import threading
import time


def arguments(parser):
    parser.add_argument('--state-root', type=Path)
    parser.add_argument('--task', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--max-bytes', type=int, default=16384)


def main(args):
    from .diagnostics import locations
    from .event_buffer import read
    from .run_io import Streams
    from .run_cli import failure
    try:
        root = (args.state_root or Path(locations(Path.home())['state'])).absolute()
        result = read(root/'events', task=args.task, session=args.session, run=args.run,
                      after=args.after, limit=args.limit, max_bytes=args.max_bytes)
        Streams(time.monotonic()+5, threading.Event()).write(
            json.dumps(result, ensure_ascii=True, separators=(',', ':'))+'\n')
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, TypeError, RecursionError):
        return failure('text', 0, 'failed', 2, 'event buffer unavailable; verify identity, cursor and private state.')
