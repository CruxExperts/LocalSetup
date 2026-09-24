"""Exercise the actual release shell boundary with offline GitHub responses."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.parametrize("response,created", [
    ("missing", True),
    ("exists", False),
    ("uncertain", False),
])
def test_release_is_only_created_as_draft_with_existing_verified_tag(tmp_path, response, created):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/publish.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    script = next(s["run"] for s in steps if s["name"] == "Prepare GitHub release draft")
    (tmp_path / "VERSION").write_text("4.22.7\n")
    for command in ("gh", "uv"):
        executable = tmp_path / command
        executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == 'uv':
    print('Verified release notes')
elif args[0] == 'api':
    mode = os.environ['RELEASE_RESPONSE']
    print('HTTP/2.0 ' + {'missing': '404 Not Found', 'exists': '200 OK', 'uncertain': '503 Unavailable'}[mode])
    sys.exit(0 if mode == 'exists' else 1)
else:
    pathlib.Path('mutation.json').write_text(json.dumps(args))
''')
        executable.chmod(0o700)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "GITHUB_REPOSITORY": "example/project", "GITHUB_SHA": "original", "RELEASE_COMMIT": "accepted",
        "RELEASE_DOCS_STATE": ".agents/state/test-release-docs",
        "RELEASE_RESPONSE": response}
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is created, result.stderr
    mutation = tmp_path / "mutation.json"
    assert mutation.exists() is created
    if created:
        args = json.loads(mutation.read_text())
        assert args[:3] == ["release", "create", "v4.22.7"]
        assert "--draft" in args and "--verify-tag" in args and "--clobber" not in args
        assert args[args.index("--target") + 1] == "accepted"
        assert "--notes-file" in args
        assert len([a for a in args if a.startswith("dist/")]) == 3


@pytest.mark.parametrize("mode,accepted", [
    ("valid", True), ("lightweight", False), ("wrong-key", False),
    ("wrong-remote", False), ("wrong-commit", False),
])
def test_release_tag_shell_requires_annotated_pinned_signature_and_exact_commit(tmp_path, mode, accepted):
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/publish.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["publish"]["steps"]
                  if step["name"] == "Verify pre-existing signed release tag and commit")
    (tmp_path / "VERSION").write_text("4.22.7\n")
    for name in ("git", "gpg", "curl"):
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n" + '''
import os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
mode = os.environ['TAG_MODE']
if name == 'curl':
    pathlib.Path(args[args.index('-o') + 1]).write_text('fixture public certificate')
elif name == 'gpg':
    pass
elif args[:2] == ['cat-file', '-t']:
    print('commit' if mode == 'lightweight' else 'tag')
elif args and args[0] == 'rev-parse':
    print('different' if mode == 'wrong-commit' and '^{commit}' in args[-1] else
          'tag-object' if args[-1] == 'refs/tags/v4.22.7' else 'accepted')
elif args and args[0] == 'ls-remote':
    peeled = args[-1].endswith('^{}')
    print(('accepted' if peeled else 'different' if mode == 'wrong-remote' else 'tag-object')
          + '\\t' + args[-1])
elif 'verify-tag' in args or 'verify-commit' in args:
    print('[GNUPG:] VALIDSIG ' + ('B' * 40 if mode == 'wrong-key' else
          'AF7968466B5B39C5E928FEFE716342D3EFBA5522'))
''')
        executable.chmod(0o700)
    state = tmp_path / "state"
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "TAG_MODE": mode, "RUNNER_TEMP": str(tmp_path), "RELEASE_DOCS_STATE": str(state),
           "RELEASE_COMMIT": "accepted"}
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is accepted, result.stderr
