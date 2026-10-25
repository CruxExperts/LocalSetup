from __future__ import annotations

import re


PEM_RE = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
ENV_LINE_RE = re.compile(r"(?im)^([A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)=.*$")
ASSIGNMENT_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password|credential)(['\"]?\s*[:=]\s*['\"]?)[^'\"\s]+")
TOKEN_URL_RE = re.compile(r"https?://([^\s/@:]+):([^\s/@]+)@")
LONG_SECRET_RE = re.compile(r"\b(?:ghp|github_pat|sk|xox[baprs])-?[A-Za-z0-9_\-]{16,}\b")
URL_RE = re.compile(r"https?://[^\s'\")>]+")
PUBLIC_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|mnt|tmp|var|etc|opt|root|run)/[^\s`'\"<>)]*")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact_text(text: str) -> str:
    redacted = PEM_RE.sub("[REDACTED_PEM_BLOCK]", text)
    redacted = ENV_LINE_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
    redacted = ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", redacted)
    redacted = TOKEN_URL_RE.sub("https://[REDACTED]@", redacted)
    redacted = LONG_SECRET_RE.sub("[REDACTED_TOKEN]", redacted)
    redacted = URL_RE.sub("[REDACTED_URL]", redacted)
    return redacted


def redact_public_text(text: str) -> str:
    """Remove private machine locations and identities from issue-bound prose."""
    redacted = redact_text(text)
    redacted = PUBLIC_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    return redacted
