"""Fixed tmux form helper. Arguments identify the channel, never its values."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import resource
import select
import socket
import time

from .command_state import process_info
from .dialog_channel import ChannelClosed, connect, encode_response, receive_frame, send_frame
from .dialog_model import Form, FormState
from .dialog_terminal import run_dialog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-record")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--controller-pid", type=int, required=True)
    parser.add_argument("--controller-starttime", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    args = parser.parse_args(argv)
    if args.session_record:
        os.environ["LOCALSETUP_TERMINAL_SESSION_RECORD"] = args.session_record
    if not math.isfinite(args.deadline) or not time.monotonic() < args.deadline <= time.monotonic() + 3600:
        return 1
    connection: socket.socket | None = None
    state: FormState | None = None
    payload = bytearray()

    def current() -> bool:
        info = process_info(args.controller_pid)
        if (time.monotonic() >= args.deadline or info is None
                or info["starttime"] != args.controller_starttime):
            return False
        return True

    def input_alive() -> bool:
        if not current():
            return False
        if connection is not None and select.select([connection], [], [], 0)[0]:
            try:
                return connection.recv(1, socket.MSG_PEEK) != b""
            except socket.timeout:
                pass
        return True

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        connection = connect(args.socket, controller_pid=args.controller_pid,
                             controller_starttime=args.controller_starttime)
        send_frame(connection, args.request_id.encode("ascii"), current)
        specification = json.loads(receive_frame(connection, current))
        if (not isinstance(specification, dict) or set(specification) != {"version", "deadline", "form"}
                or type(specification["version"]) is not int or specification["version"] != 1
                or specification["deadline"] != args.deadline):
            raise ChannelClosed("Form protocol does not match this helper.")
        form = Form.from_mapping(specification["form"])
        # The controller has admitted this process and holds a generation-bound
        # pidfd before sending the form. Protect helper memory before any input.
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:  # Linux PR_SET_DUMPABLE
            raise ChannelClosed("Private input protection is unavailable.")
        state = FormState(form, deadline=args.deadline)
        status = run_dialog(state, alive=input_alive,
                            on_visible=lambda: send_frame(connection, b"visible", current))
        if status != "submitted":
            if current():
                send_frame(connection, status.encode("ascii"), current)
                receive_frame(connection, current, maximum=16)
            return 0
        response = state.take_response()
        try:
            payload = encode_response(form, response)
            send_frame(connection, payload, current)
        finally:
            response.dispose()
            payload[:] = b"\0" * len(payload)
            payload.clear()
        # A missing acknowledgement never causes retransmission.
        return 0 if receive_frame(connection, current, maximum=16) == b"accepted" else 2
    except Exception:
        # Do not serialize arbitrary exception details from a private renderer.
        return 1
    finally:
        payload[:] = b"\0" * len(payload)
        payload.clear()
        if state is not None:
            state.close("failed")
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
