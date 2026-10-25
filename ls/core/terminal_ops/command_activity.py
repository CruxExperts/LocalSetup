"""Bounded client activity observations; never execution or approval authority."""
from collections import OrderedDict
import re

KINDS = {"commandExecution": "shell", "mcpToolCall": "tool", "dynamicToolCall": "tool",
         "collabAgentToolCall": "agents", "webSearch": "web", "fileChange": "edit",
         "imageView": "image", "imageGeneration": "image"}
STATES = {"inProgress": "Running", "completed": "Completed", "failed": "Failed",
          "declined": "Declined", "cancelled": "Cancelled", "interrupted": "Interrupted"}
TERMINAL = {"Completed", "Failed", "Declined", "Cancelled", "Interrupted"}


def observation(thread_id, entry):
    """Discard messages, reasoning, commands, arguments, output and descriptions."""
    item = entry.get("item")
    if not isinstance(item, dict) or item.get("type") not in KINDS:
        return None
    identity = item.get("id")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
        raise ValueError("invalid activity identity")
    kind = KINDS[item["type"]]
    name = item.get("tool") if kind in {"tool", "agents"} else kind
    name = name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,64}", name) else kind
    state = STATES.get(item.get("status"), "Unknown")
    if state == "Unknown" and type(entry.get("completedAtMs")) is int:
        state = "Completed"
    return {"thread": thread_id, "id": identity, "kind": kind, "name": name, "state": state}


class Activity:
    def __init__(self, maximum=2048):
        self.maximum = maximum
        self.items = OrderedDict()
        self.dropped = 0

    def accept(self, value):
        key = value["thread"], value["id"]
        old = self.items.get(key)
        if old == value or old and old["state"] in TERMINAL and value["state"] not in TERMINAL:
            return False
        self.items[key] = value
        self.items.move_to_end(key)
        if len(self.items) > self.maximum:
            self.items.popitem(last=False)
            self.dropped += 1
        return True
