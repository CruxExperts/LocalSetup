from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "ls" / "skills" / "ls-github-starredrepos-sync"


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def node_child_pipe_capture_supported() -> bool:
    if not shutil.which("node"):
        return False
    probe = (
        "import { spawn } from 'node:child_process';"
        "const child = spawn(process.execPath, ['-e', "
        "'process.stdin.resume(); process.stdin.on(\\\"end\\\", () => process.stdout.write(\\\"ok\\\"))'], "
        "{ stdio: ['pipe', 'pipe', 'pipe'] });"
        "let stdout = '';"
        "child.stdout.on('data', chunk => { stdout += chunk; });"
        "child.on('error', () => process.exit(2));"
        "child.on('close', code => process.exit(code === 0 && stdout === 'ok' ? 0 : 1));"
        "child.stdin.end();"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", probe],
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def test_github_starredrepos_skill_is_registered() -> None:
    skill_md = SKILL / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "ls-github-starredrepos-sync"
    assert "starred repositories archive" in frontmatter["description"]

    pack = yaml.safe_load((ROOT / "ls" / "config" / "pack.yaml").read_text(encoding="utf-8"))
    assert "ls-github-starredrepos-sync" in pack["packs"]["integrations"]

    smoke = yaml.safe_load((ROOT / "ls" / "tests" / "skill_smoke_commands.yaml").read_text(encoding="utf-8"))
    assert smoke["ls-github-starredrepos-sync"] == "node scripts/verify-starredrepos-state.mjs --help"


def test_github_starredrepos_required_files_exist() -> None:
    required = [
        "references/source-ledger.md",
        "references/architecture.md",
        "references/authenticated-github-context.md",
        "references/starred-repo-sync-runbook.md",
        "references/starredrepos-repository-contract.md",
        "references/storage-strategies.md",
        "references/scout-modes.md",
        "references/release-intelligence.md",
        "references/docs-archive.md",
        "references/api-cli-references.md",
        "references/node-runtime.md",
        "references/actions-automation.md",
        "references/security-privacy.md",
        "references/rate-limits-resilience.md",
        "references/troubleshooting.md",
        "references/update-procedure.md",
        "data/schema/manifest.schema.json",
        "data/schema/snapshot-diff.schema.json",
        "data/schema/repo-metadata.schema.json",
        "data/schema/scout-report.schema.json",
        "data/examples/manifest.example.json",
        "data/examples/snapshot-diff.example.json",
        "data/examples/repo-metadata.example.json",
        "data/examples/scout-report.example.json",
        "templates/scout-prompt.md",
        "templates/scout-result.md",
        "templates/repo-doc.md",
        "templates/archive-README.md",
        "templates/sync-summary.md",
        "templates/github-actions-starredrepos.yml",
        "scripts/verify-github-auth.mjs",
        "scripts/list-starred-repos.mjs",
        "scripts/sync-starredrepos.mjs",
        "scripts/scout-repo-metadata.mjs",
        "scripts/generate-starredrepos-docs.mjs",
        "scripts/verify-starredrepos-state.mjs",
    ]
    missing = [path for path in required if not (SKILL / path).is_file()]
    assert missing == []


def test_github_starredrepos_json_examples_parse_and_validate_shape() -> None:
    repo = load_json(SKILL / "data/examples/repo-metadata.example.json")
    assert repo["fullName"] == "octocat/Hello-World"
    assert repo["owner"] == "octocat"
    assert isinstance(repo["topics"], list)

    manifest = load_json(SKILL / "data/examples/manifest.example.json")
    assert manifest["schemaVersion"] == "1.0"
    assert manifest["repositoryCount"] == len(manifest["repositories"])
    assert manifest["repositories"][0]["fullName"] == repo["fullName"]

    diff = load_json(SKILL / "data/examples/snapshot-diff.example.json")
    assert diff["schemaVersion"] == "1.0"
    assert diff["added"] == ["octocat/Hello-World"]

    scout = load_json(SKILL / "data/examples/scout-report.example.json")
    assert scout["mode"] == "static"
    assert {claim["status"] for claim in scout["claims"]} <= {"verified", "unverified"}


def test_github_starredrepos_script_help_exits_cleanly() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    scripts = [
        "verify-github-auth.mjs",
        "list-starred-repos.mjs",
        "sync-starredrepos.mjs",
        "scout-repo-metadata.mjs",
        "generate-starredrepos-docs.mjs",
        "verify-starredrepos-state.mjs",
    ]
    for script in scripts:
        result = subprocess.run(
            ["node", f"scripts/{script}", "--help"],
            cwd=SKILL,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout


def test_github_starredrepos_source_ledger_dates_volatile_claims() -> None:
    text = (SKILL / "references/source-ledger.md").read_text(encoding="utf-8")
    assert "Accessed: 2026-09-02." in text
    assert "Planning Verification Pattern" in text
    assert "CruxExperts" not in text
    assert "22 starred" not in text
    for phrase in ["Verify `gh auth status", "Verify `gh api /versions", "Verify GraphQL star counts"]:
        assert phrase in text


def test_github_starredrepos_files_do_not_contain_token_like_strings() -> None:
    token_re = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9_.-]{20,})\b")
    offenders: list[str] = []
    for base in [SKILL, ROOT / "ls" / "docs" / "_generated"]:
        for path in base.glob("**/*"):
            if path.is_file() and path.suffix in {".json", ".md", ".mjs", ".yml"}:
                if token_re.search(path.read_text(encoding="utf-8")):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_github_starredrepos_data_files_are_not_ignored() -> None:
    required = [
        SKILL / "data/schema/manifest.schema.json",
        SKILL / "data/examples/manifest.example.json",
    ]
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *[str(path.relative_to(ROOT)) for path in required]],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    listed = set(result.stdout.splitlines())
    assert {str(path.relative_to(ROOT)) for path in required} <= listed


def test_github_starredrepos_rejects_malicious_full_name(tmp_path: Path) -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    manifest = load_json(SKILL / "data/examples/manifest.example.json")
    manifest["repositories"][0]["fullName"] = "owner/repo/../../../../escape"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = subprocess.run(
        [
            "node",
            "scripts/generate-starredrepos-docs.mjs",
            "--manifest",
            str(manifest_path),
            "--out",
            str(tmp_path / "out"),
        ],
        cwd=SKILL,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "must be exactly owner/name" in result.stderr
    assert not (tmp_path / "escape.md").exists()


def test_github_starredrepos_rejects_malicious_scout_full_name(tmp_path: Path) -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    if not node_child_pipe_capture_supported():
        pytest.skip("node cannot capture a piped child process after stdin closes, required by command scout execution")
    report = load_json(SKILL / "data/examples/scout-report.example.json")
    report["fullName"] = "../escape"
    command = tmp_path / "scout-command.mjs"
    command.write_text(
        "#!/usr/bin/env node\n"
        "process.stdin.resume();\n"
        f"process.stdin.on('end', () => process.stdout.write({json.dumps(json.dumps(report))}));\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    result = subprocess.run(
        ["node", "scripts/scout-repo-metadata.mjs", "--input", "data/examples/repo-metadata.example.json", "--mode", "command"],
        cwd=SKILL,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
        env={**dict(), **__import__("os").environ, "STARREDREPOS_SCOUT_COMMAND": f"node {command}"},
    )
    assert result.returncode != 0
    assert "must not be path traversal segments" in result.stderr


def test_github_starredrepos_honors_github_host_environment() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = (
        "import { GITHUB_HOST, githubEnv } from './scripts/common.mjs';"
        "console.log(JSON.stringify({host:GITHUB_HOST, ghHost:githubEnv().GH_HOST}));"
    )
    for env_key in ("STARREDREPOS_GITHUB_HOST", "GH_HOST"):
        env = {**__import__("os").environ}
        env.pop("STARREDREPOS_GITHUB_HOST", None)
        env.pop("GH_HOST", None)
        env[env_key] = "ghe.example"
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=SKILL,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload == {"host": "ghe.example", "ghHost": "ghe.example"}


def test_github_starredrepos_storage_mode_defaults_to_metadata() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    text = (SKILL / "scripts/sync-starredrepos.mjs").read_text(encoding="utf-8")
    assert 'process.env.STARREDREPOS_STORAGE_MODE || "metadata"' in text
    assert "Current helper apply mode is metadata-only" in text

    schema = load_json(SKILL / "data/schema/manifest.schema.json")
    assert schema["properties"]["storageMode"]["enum"] == ["metadata"]


def test_github_starredrepos_rejects_unsupported_storage_modes() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")

    for mode in ("submodule", "vendor", "checkout-cache", "bare-mirror-cache"):
        result = subprocess.run(
            ["node", "scripts/sync-starredrepos.mjs", "--dry-run"],
            cwd=SKILL,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
            env={**__import__("os").environ, "STARREDREPOS_STORAGE_MODE": mode},
        )
        assert result.returncode != 0
        assert result.stdout == ""
        assert f"Unsupported storage mode: {mode}" in result.stderr


def test_github_starredrepos_redaction_helper_covers_github_tokens() -> None:
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    script = SKILL / "scripts/test_github_starredrepos_redaction.mjs"
    result = subprocess.run(
        ["node", str(script)],
        cwd=SKILL,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_github_starredrepos_initializes_only_a_fresh_archive_git_root() -> None:
    if not shutil.which("node") or not shutil.which("git"):
        pytest.skip("node and git are required")

    script = (
        "import { mkdtemp, rm } from 'node:fs/promises';"
        "import { tmpdir } from 'node:os';"
        "import { join } from 'node:path';"
        "import { ensureGitRepository } from './scripts/common.mjs';"
        "const worktree = await mkdtemp(join(tmpdir(), 'starredrepos-git-'));"
        "try {"
        "const first = await ensureGitRepository(worktree);"
        "const second = await ensureGitRepository(worktree);"
        "if (first !== true || second !== false) process.exit(2);"
        "process.stdout.write('ok\\n');"
        "} finally { await rm(worktree, { recursive: true, force: true }); }"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=SKILL,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


def test_github_starredrepos_rejects_nested_worktrees_even_with_git_error_text_in_path(tmp_path: Path) -> None:
    if not shutil.which("node") or not shutil.which("git"):
        pytest.skip("node and git are required")
    outer = tmp_path / "outer"
    nested = outer / "not a git repository"
    subprocess.run(["git", "init", str(outer)], check=True, text=True, capture_output=True)
    nested.mkdir()
    script = (
        "import { ensureGitRepository } from './scripts/common.mjs';"
        f"const worktree = {json.dumps(str(nested))};"
        "try { await ensureGitRepository(worktree); process.exit(2); }"
        "catch (error) { process.exit(String(error.message).includes('Git repository root') ? 0 : 3); }"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=SKILL,
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not (nested / ".git").exists()


def test_github_starredrepos_remote_creation_push_sets_upstream(tmp_path: Path) -> None:
    if not shutil.which("node") or not shutil.which("git"):
        pytest.skip("node and git are required")
    worktree = tmp_path / "archive"
    remote = tmp_path / "archive.git"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *' /user/starred?per_page=100'*) printf 'HTTP/1.1 200 OK\n\n[]\n' ;;\n"
        "  *' /user'*) printf '{\"login\":\"test-user\"}\n' ;;\n"
        "  *'repo create '*) git init --bare \"$FAKE_GH_REMOTE\" >/dev/null && git -C \"$FAKE_GH_WORKTREE\" remote add origin \"$FAKE_GH_REMOTE\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STARREDREPOS_WORKTREE": str(worktree),
        "FAKE_GH_REMOTE": str(remote),
        "FAKE_GH_WORKTREE": str(worktree),
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    result = subprocess.run(
        ["node", "scripts/sync-starredrepos.mjs", "--apply", "--create-remote", "--commit", "--push"],
        cwd=SKILL,
        env=env,
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=worktree,
        check=True,
        text=True,
        capture_output=True,
    )
    assert upstream.stdout.strip().startswith("origin/")

def test_github_starredrepos_mutation_paths_require_git_root_and_sha_pinned_actions() -> None:
    sync = (SKILL / "scripts/sync-starredrepos.mjs").read_text(encoding="utf-8")
    assert "if (options.create_remote || options.commit)" in sync
    assert sync.index("await ensureGitRepository(worktree)") < sync.index('"repo", "create"')
    assert sync.index("await ensureGitRepository(worktree)") < sync.index('["add", "manifest.json"')

    template = (SKILL / "templates/github-actions-starredrepos.yml").read_text(encoding="utf-8")
    uses_lines = [line.strip() for line in template.splitlines() if "uses:" in line]
    assert uses_lines == [
        "- uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6.1.0",
        "- uses: actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4.4.0",
    ]
    assert all(re.fullmatch(r"- uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+", line) for line in uses_lines)
