"""Safe errors shared by the Herdr operations core and CLI."""

from __future__ import annotations

import re


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
EFFECTS = frozenset({"none", "unknown", "applied"})


def safe_text(value: object, *, limit: int = 500) -> str:
    text = _CONTROL.sub(" ", str(value)).strip()
    return text[:limit] or "Herdr operation failed."


class HerdrOpsError(Exception):
    """An actionable error with the effect of any attempted mutation."""

    def __init__(self, code: str, message: str, effect: str = "none", *, operation_id: str | None = None) -> None:
        self.code = safe_text(code, limit=80)
        self.message = safe_text(message)
        self.effect = effect if effect in EFFECTS else "unknown"
        self.operation_id = operation_id
        super().__init__(self.message)

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message, "effect": self.effect}
        if self.operation_id:
            result["operation_id"] = self.operation_id
        return result
