"""Diff analysis helpers for pr_review.py."""

import re
import shutil
import subprocess
from pathlib import Path


# Pattern lists: (regex, category, message)
SECRET_PATTERNS = [
    (r"(?i)(password|passwd|secret|api[_-]?key|token|auth)\s*[:=]\s*[\"'][^\"']{8,}[\"']", "SECURITY", "Possible hardcoded credential/secret"),
    (r"(?i)AWS[_A-Z]*KEY\s*[:=]", "SECURITY", "Possible hardcoded AWS key"),
    (r"(?i)-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY", "SECURITY", "Private key in source code"),
]
GO_PATTERNS = [
    (r"^\+.*,\s*_\s*:?=.*\(", "ERROR_HANDLING", "Discarded error return value (Go)"),
    (r"^\+.*\.Close\(\)\s*$", "ERROR_HANDLING", "Unchecked Close()"),
    (r"^\+.*panic\(", "RISK", "Direct panic() call"),
    (r"^\+.*fmt\.Print", "STYLE", "fmt.Print in production code"),
    (r"^\+.*os\.Exit\(", "RISK", "Direct os.Exit()"),
]
PYTHON_PATTERNS = [
    (r"^\+.*except\s*:", "ERROR_HANDLING", "Bare except clause"),
    (r"^\+.*except Exception:", "ERROR_HANDLING", "Broad except Exception"),
    (r"^\+.*print\(", "STYLE", "print() in production code"),
    (r"^\+.*# type: ignore", "TYPING", "Type ignore comment"),
]
JS_PATTERNS = [
    (r"^\+.*console\.log\(", "STYLE", "console.log in production code"),
    (r"^\+.*debugger", "STYLE", "Debugger statement"),
    (r"^\+.*process\.exit\(", "RISK", "Direct process.exit()"),
    (r"^\+.*eval\(", "SECURITY", "eval() usage"),
    (r"^\+.*\bany\b", "TYPING", "TypeScript any type"),
]
GENERAL_PATTERNS = [
    (r"^\+.*TODO", "TODO", "TODO marker"),
    (r"^\+.*FIXME", "TODO", "FIXME marker"),
    (r"^\+.*HACK", "TODO", "HACK marker"),
    (r"^\+.*XXX", "TODO", "XXX marker"),
    (r"^\+.{200,}", "STYLE", "Very long line (>200 chars)"),
]


def categorize_files(files: list[str]) -> dict[str, list[str]]:
    from collections import defaultdict
    cats = defaultdict(list)
    for f in files:
        if not f:
            continue
        ext = f.rsplit(".", 1)[-1] if "." in f else ""
        if ext == "go":
            cats["go"].append(f)
        elif ext == "py":
            cats["python"].append(f)
        elif ext in ("ts", "tsx", "js", "jsx"):
            cats["frontend"].append(f)
        elif ext in ("yml", "yaml", "toml", "json", "env"):
            cats["config"].append(f)
        elif ext in ("md", "txt", "rst"):
            cats["docs"].append(f)
        elif ext == "sql":
            cats["sql"].append(f)
        elif "Dockerfile" in f or f == "docker-compose.yml":
            cats["docker"].append(f)
        elif f.startswith(".github/"):
            cats["ci"].append(f)
        else:
            cats["other"].append(f)
    return dict(cats)


def analyze_diff(diff_text: str) -> list[dict]:
    findings = []
    current_file = None
    line_num = 0
    for line in diff_text.split("\n"):
        m = re.match(r"^\+\+\+ b/(.*)", line)
        if m:
            current_file = m.group(1)
            continue
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if m:
            line_num = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            line_num += 1
            all_p = SECRET_PATTERNS + GENERAL_PATTERNS
            if current_file:
                if current_file.endswith(".go"):
                    all_p = all_p + GO_PATTERNS
                elif current_file.endswith(".py"):
                    all_p = all_p + PYTHON_PATTERNS
                elif current_file.endswith((".js", ".jsx", ".ts", ".tsx")):
                    all_p = all_p + JS_PATTERNS
            for pat, cat, msg in all_p:
                if re.search(pat, line):
                    findings.append({
                        "file": current_file or "unknown",
                        "line": line_num,
                        "category": cat,
                        "message": msg,
                        "context": (line[1:].strip())[:120],
                    })
        elif not line.startswith("-"):
            line_num += 1
    return findings


def check_test_coverage(files: list[str]) -> str:
    src, tests = [], []
    for f in files:
        f = f.strip()
        if not f:
            continue
        name = f.split("/")[-1]
        if f.endswith("_test.go") or name.startswith("test_") or f.endswith("_test.py") or ".test." in f or ".spec." in f:
            tests.append(f)
        elif f.endswith((".go", ".py", ".ts", ".tsx", ".js", ".jsx")):
            src.append(f)
    missing = []
    for s in src:
        has_test = False
        s_dir = "/".join(s.split("/")[:-1])
        s_name = s.split("/")[-1].rsplit(".", 1)[0]
        for t in tests:
            t_dir = "/".join(t.split("/")[:-1])
            if t_dir == s_dir or f"test_{s_name}" in t or f"{s_name}_test" in t or f"{s_name}.test" in t or f"{s_name}.spec" in t:
                has_test = True
                break
        skip = any(k in s for k in ["__init__", "main.go", "main.py", "config", "types", "models", "schema", "index.ts", "index.js"])
        if not has_test and not skip:
            missing.append(s)
    if missing:
        return "Files without corresponding test changes:\n" + "\n".join(f"  - {f}" for f in missing)
    return "Test coverage looks adequate for changed files."


def run_local_lint(files: list[str], local_dir: Path | None) -> str:
    if not local_dir or not local_dir.is_dir():
        return ""
    out_parts = []
    go_files = [f for f in files if f.endswith(".go")]
    if go_files and _which("golangci-lint"):
        dirs = sorted(set("/".join(f.split("/")[:-1]) for f in go_files))
        for d in dirs:
            full = local_dir / d
            if full.is_dir():
                r = subprocess.run(
                    ["golangci-lint", "run", "--timeout", "2m", "--new-from-rev=HEAD~1"],
                    cwd=str(full),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if r.stdout or r.stderr:
                    out_parts.append(f"### golangci-lint ({d})\n```\n{r.stdout or r.stderr}\n```")
    py_files = [f for f in files if f.endswith(".py")]
    if py_files and _which("ruff"):
        paths = [str(local_dir / f) for f in py_files]
        r = subprocess.run(["ruff", "check"] + paths, capture_output=True, text=True, timeout=60, cwd=str(local_dir))
        if r.returncode != 0 and r.stdout and "All checks passed" not in r.stdout:
            out_parts.append("### ruff\n```\n" + (r.stdout or r.stderr or "") + "\n```")
    return "\n\n".join(out_parts) if out_parts else ""


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None
