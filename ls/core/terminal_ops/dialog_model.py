"""Bounded, in-memory form state shared by terminal dialog adapters.

This module grants no execution or authentication authority. Consumers own the
request binding, delivery, and cleanup; values must never enter shell receipts.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_KINDS = {"password", "text", "multiline", "checkbox", "radio", "confirm"}
_EDITABLE = {"password", "text", "multiline", "confirm"}


def _text(value: object, maximum: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Dialog text must be a string.")
    try:
        size = len(value.encode("utf-8", "strict"))
    except UnicodeError:
        raise ValueError("Dialog text contains invalid Unicode.") from None
    if size > maximum or any(
        unicodedata.category(ch).startswith("C")
        and not (multiline and ch in "\n\t") for ch in value
    ):
        raise ValueError("Dialog text contains controls or exceeds its limit.")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Dialog identifier is invalid.")
    return value


@dataclass(frozen=True, slots=True)
class Choice:
    value: str
    label: str

    def __post_init__(self) -> None:
        _identifier(self.value)
        _text(self.label, 256)


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    label: str
    kind: str = "text"
    required: bool = False
    max_bytes: int = 4096
    max_lines: int = 32
    choices: tuple[Choice, ...] = ()
    initial: str | tuple[str, ...] | None = field(default=None, repr=False)
    confirmation: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name)
        _text(self.label, 256)
        if not isinstance(self.kind, str) or self.kind not in _KINDS or type(self.required) is not bool:
            raise ValueError("Dialog field type is invalid.")
        if self.initial is None:
            object.__setattr__(self, "initial", "" if self.kind in _EDITABLE else ())
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 16384:
            raise ValueError("Dialog byte limit is invalid.")
        if type(self.max_lines) is not int or not 1 <= self.max_lines <= 64:
            raise ValueError("Dialog line limit is invalid.")
        if not isinstance(self.choices, tuple) or any(not isinstance(c, Choice) for c in self.choices):
            raise ValueError("Dialog choices must be immutable choice records.")
        if self.kind in _EDITABLE:
            if self.choices:
                raise ValueError("Text fields cannot have choices.")
            _text(self.initial, self.max_bytes, multiline=self.kind == "multiline")
            if self.initial.count("\n") + 1 > self.max_lines:
                raise ValueError("Initial text exceeds the line limit.")
            if self.kind in {"password", "confirm"} and self.initial:
                raise ValueError("Password and confirmation fields cannot have defaults.")
        else:
            if not 1 <= len(self.choices) <= 128:
                raise ValueError("Choice count is outside the supported range.")
            names = {choice.value for choice in self.choices}
            if len(names) != len(self.choices):
                raise ValueError("Choice identifiers must be unique.")
            if not isinstance(self.initial, tuple) or any(not isinstance(value, str) or value not in names for value in self.initial):
                raise ValueError("Initial selection is invalid.")
            if len(set(self.initial)) != len(self.initial) or (self.kind == "radio" and len(self.initial) > 1):
                raise ValueError("Initial selection is invalid.")
        if self.kind == "confirm":
            if not self.confirmation:
                raise ValueError("Confirmation requires an exact expected phrase.")
            _text(self.confirmation, self.max_bytes)
        elif self.confirmation is not None:
            raise ValueError("Only confirmation fields accept an expected phrase.")


@dataclass(frozen=True, slots=True)
class Form:
    title: str
    fields: tuple[Field, ...]
    description: str = ""

    def __post_init__(self) -> None:
        _text(self.title, 256)
        _text(self.description, 2048, multiline=True)
        if (not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 32
                or any(not isinstance(item, Field) for item in self.fields)):
            raise ValueError("Dialog fields are outside the supported range.")
        if len({item.name for item in self.fields}) != len(self.fields):
            raise ValueError("Dialog field identifiers must be unique.")
        if sum(item.max_bytes for item in self.fields if item.kind in _EDITABLE) > 65536:
            raise ValueError("Dialog exceeds its aggregate input limit.")
        if sum(len(item.choices) for item in self.fields) > 256:
            raise ValueError("Dialog exceeds its aggregate choice limit.")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Form:
        """Parse a declarative request without callbacks or executable validators."""
        if not isinstance(value, Mapping) or set(value) - {"title", "description", "fields"}:
            raise ValueError("Dialog schema contains unsupported members.")
        raw = value.get("fields")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
            raise ValueError("Dialog fields are outside the supported range.")
        fields = []
        allowed = {"name", "label", "kind", "required", "max_bytes", "max_lines", "choices", "initial", "confirmation"}
        for item in raw:
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError("Dialog field contains unsupported members.")
            options = item.get("choices", [])
            if not isinstance(options, list) or len(options) > 128:
                raise ValueError("Dialog choices are invalid.")
            choices = []
            for choice in options:
                if not isinstance(choice, dict) or set(choice) != {"value", "label"}:
                    raise ValueError("Dialog choice is invalid.")
                choices.append(Choice(**choice))
            data = dict(item, choices=tuple(choices))
            if isinstance(data.get("kind"), str) and data["kind"] in {"radio", "checkbox"}:
                initial = data.get("initial", [])
                if not isinstance(initial, list):
                    raise ValueError("Dialog initial selection must be a list.")
                data["initial"] = tuple(initial)
            try:
                fields.append(Field(**data))
            except TypeError:
                raise ValueError("Dialog field is incomplete.") from None
        return cls(title=value.get("title"), description=value.get("description", ""), fields=tuple(fields))


def _wipe(value: bytearray) -> None:
    value[:] = b"\0" * len(value)
    value.clear()


@dataclass(slots=True, repr=False)
class Response:
    """One private delivery. The consumer must dispose after its single handoff."""

    values: dict[str, bytearray | tuple[str, ...]]

    def __repr__(self) -> str:
        return "<DialogResponse private>"

    def dispose(self) -> None:
        for value in self.values.values():
            if isinstance(value, bytearray):
                _wipe(value)
        self.values.clear()


def validate_response(form: Form, response: Response) -> tuple[int, str] | None:
    """Apply the same bounded validation to local and privately received values."""
    if set(response.values) != {item.name for item in form.fields}:
        return 0, "The response fields do not match the form."
    for index, item in enumerate(form.fields):
        value = response.values[item.name]
        if item.kind in _EDITABLE:
            if not isinstance(value, bytearray) or len(value) > item.max_bytes:
                return index, "Input exceeds the field limit."
            try:
                decoded = _text(value.decode("utf-8"), item.max_bytes, multiline=item.kind == "multiline")
            except (UnicodeError, ValueError):
                return index, "Input contains unsupported characters."
            if decoded.count("\n") + 1 > item.max_lines:
                return index, "Input exceeds the field limit."
            if item.kind == "confirm" and decoded != item.confirmation:
                return index, "The confirmation phrase does not match."
        elif (not isinstance(value, tuple) or len(value) > len(item.choices)
              or any(not isinstance(part, str) or part not in {c.value for c in item.choices} for part in value)
              or len(set(value)) != len(value) or (item.kind == "radio" and len(value) > 1)):
            return index, "The selection is invalid."
        if item.required and not value:
            return index, "A required field is empty."
    return None


class FormState:
    """No I/O, persistence, command dispatch, or secret-bearing representation."""

    def __init__(self, form: Form, *, deadline: float, clock: Callable[[], float] = time.monotonic):
        if not isinstance(form, Form) or type(deadline) not in {int, float} or not math.isfinite(deadline):
            raise ValueError("Dialog requires a form and a finite deadline.")
        self.form, self.deadline, self.clock = form, deadline, clock
        self.status, self.error = "editing", ""
        self.focus = 0
        self.width, self.height = 0, 0
        self._buffers = [bytearray(f.initial.encode()) if f.kind in _EDITABLE else bytearray() for f in form.fields]
        self._selections = [set(f.initial) if f.kind not in _EDITABLE else set() for f in form.fields]
        self.cursors = [len(f.initial) if f.kind in _EDITABLE else 0 for f in form.fields]

    def __repr__(self) -> str:
        return f"<FormState status={self.status}>"

    @property
    def fits(self) -> bool:
        return self.width >= 40 and self.height >= 10

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = max(0, width), max(0, height)
        self.tick()

    def tick(self) -> str:
        if self.status in {"editing", "submitted"} and self.clock() >= self.deadline:
            self.close("expired")
        return self.status

    def close(self, status: str = "cancelled") -> None:
        if status not in {"cancelled", "expired", "unavailable", "failed"}:
            raise ValueError("Dialog close status is invalid.")
        for value in self._buffers:
            _wipe(value)
        for selected in self._selections:
            selected.clear()
        self.cursors = [0] * len(self.cursors)
        if self.status in {"editing", "submitted"}:
            self.status = status
        self.error = ""

    def display(self, index: int) -> str:
        """For the private renderer only; password display never reveals length."""
        if self.form.fields[index].kind == "password":
            return "[hidden]"
        return self._buffers[index].decode("utf-8")

    def selected(self, index: int, value: str) -> bool:
        return value in self._selections[index]

    def insert(self, text: str) -> bool:
        """Insert literal input/paste. Control characters never become actions."""
        if self.tick() != "editing" or self.focus >= len(self.form.fields):
            return False
        item = self.form.fields[self.focus]
        if item.kind not in _EDITABLE:
            return False
        previous = self._buffers[self.focus].decode("utf-8")
        cursor = self.cursors[self.focus]
        try:
            _text(text, item.max_bytes, multiline=item.kind == "multiline")
            updated = previous[:cursor] + text + previous[cursor:]
            _text(updated, item.max_bytes, multiline=item.kind == "multiline")
            if updated.count("\n") + 1 > item.max_lines:
                raise ValueError
        except (ValueError, TypeError):
            self.error = "Input contains unsupported characters or exceeds the field limit."
            return False
        self._replace(self.focus, updated, cursor + len(text))
        return True

    def _replace(self, index: int, text: str, cursor: int) -> None:
        _wipe(self._buffers[index])
        self._buffers[index].extend(text.encode("utf-8"))
        self.cursors[index], self.error = cursor, ""

    def key(self, key: str) -> str:
        if self.tick() != "editing":
            return self.status
        count = len(self.form.fields)
        if key in {"escape", "cancel"}:
            self.close()
        elif key in {"tab", "backtab"}:
            self.focus = (self.focus + (1 if key == "tab" else -1)) % (count + 2)
        elif key == "submit" or (key == "enter" and self.focus == count):
            self._submit()
        elif key == "enter" and self.focus == count + 1:
            self.close()
        elif self.focus < count:
            self._field_key(key)
        return self.status

    def _field_key(self, key: str) -> None:
        index = self.focus
        item, cursor = self.form.fields[index], self.cursors[index]
        if item.kind not in _EDITABLE:
            if key in {"up", "left", "down", "right"}:
                self.cursors[index] = (cursor + (-1 if key in {"up", "left"} else 1)) % len(item.choices)
            elif key == "space":
                value = item.choices[cursor].value
                selected = self._selections[index]
                if item.kind == "radio":
                    selected.clear()
                    selected.add(value)
                elif value in selected:
                    selected.remove(value)
                else:
                    selected.add(value)
                self.error = ""
            elif key == "enter":
                self.focus += 1
            return
        value = self._buffers[index].decode("utf-8")
        if key == "backspace" and cursor:
            self._replace(index, value[:cursor - 1] + value[cursor:], cursor - 1)
        elif key == "delete" and cursor < len(value):
            self._replace(index, value[:cursor] + value[cursor + 1:], cursor)
        elif key in {"left", "right"}:
            self.cursors[index] = max(0, min(len(value), cursor + (-1 if key == "left" else 1)))
        elif key == "home":
            self.cursors[index] = value.rfind("\n", 0, cursor) + 1
        elif key == "end":
            end = value.find("\n", cursor)
            self.cursors[index] = len(value) if end < 0 else end
        elif key in {"up", "down"} and item.kind == "multiline":
            start = value.rfind("\n", 0, cursor) + 1
            column = cursor - start
            if key == "up" and start:
                previous = value.rfind("\n", 0, start - 1) + 1
                self.cursors[index] = min(start - 1, previous + column)
            elif key == "down":
                end = value.find("\n", cursor)
                if end >= 0:
                    after = value.find("\n", end + 1)
                    self.cursors[index] = min(len(value) if after < 0 else after, end + 1 + column)
        elif key == "enter":
            if item.kind == "multiline":
                self.insert("\n")
            else:
                self.focus += 1
        elif key == "space":
            self.insert(" ")

    def _submit(self) -> None:
        if not self.fits:
            self.error = "Enlarge the terminal to submit."
            return
        values = {
            item.name: self._buffers[index] if item.kind in _EDITABLE else tuple(self._selections[index])
            for index, item in enumerate(self.form.fields)
        }
        invalid = validate_response(self.form, Response(values))
        if invalid:
            self.focus, self.error = invalid
            return
        self.status, self.error = "submitted", ""

    def take_response(self) -> Response:
        """Transfer ownership once; submission is not authentication success."""
        if self.tick() != "submitted":
            raise ValueError("Dialog has no undelivered submission.")
        values: dict[str, bytearray | tuple[str, ...]] = {}
        for index, item in enumerate(self.form.fields):
            if item.kind in _EDITABLE:
                values[item.name], self._buffers[index] = self._buffers[index], bytearray()
            else:
                values[item.name] = tuple(c.value for c in item.choices if c.value in self._selections[index])
                self._selections[index].clear()
        self.cursors = [0] * len(self.cursors)
        self.status = "delivered"
        return Response(values)
