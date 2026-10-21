"""Run inside a disposable container with /source read-only and /state persistent.

Invoke twice in separate containers, with ``install`` then ``recreate``. Dependencies
and git must already be available; this probe never installs host prerequisites.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


SOURCE = Path('/source')
STATE = Path('/state')
SKILL = 'ls-docker-build-compose'


def command(home: Path, project: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SOURCE / 'ls/tools/localsetup.py'),
         '--source-root', str(SOURCE), '--home', str(home),
         '--target-directory', str(project), *args],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, 'HOME': str(home)},
    )
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr[-2000:]} {result.stdout[-2000:]}')
    return json.loads(result.stdout)


def run(phase: str) -> None:
    for mode in ('symlink', 'portable'):
        root = STATE / mode
        home, project = root / 'home', root / 'project'
        if phase == 'install':
            home.mkdir(parents=True, exist_ok=True)
            project.mkdir(parents=True, exist_ok=True)
            result = command(home, project, 'install', '--preset', 'custom',
                             '--skills', SKILL, '--platforms', 'codex', 'claude-code',
                             '--mode', mode, '--skill-scope', 'both',
                             '--dependency-mode', 'prompt-only', '--apply')
            assert result['ok'], result
        manifest = json.loads((home / '.local/share/localsetup/paths.json').read_text())
        assert '/source' in json.dumps(manifest)
        paths = [home / '.agents/skills' / SKILL / 'SKILL.md',
                 project / '.agents/skills' / SKILL / 'SKILL.md',
                 home / '.claude/skills' / SKILL / 'SKILL.md',
                 project / '.claude/skills' / SKILL / 'SKILL.md']
        before = [(p.read_bytes(), p.stat().st_uid, p.stat().st_gid) for p in paths]
        for _ in range(2):
            result = command(home, project, 'update', '--dependency-mode', 'prompt-only')
            assert result.get('ok'), result
        after = [(p.read_bytes(), p.stat().st_uid, p.stat().st_gid) for p in paths]
        assert before == after, 'refresh changed package content or ownership'
        result = command(home, project, 'verify', '--level', 'filesystem')
        assert result.get('ok'), result
        result = command(home, project, 'adapters', 'check')
        assert result.get('ok'), result
        # Retain the report and reject blocked installations.
        result = subprocess.run(
            [sys.executable, str(SOURCE / 'ls/tools/localsetup.py'),
             '--source-root', str(SOURCE), '--home', str(home),
             '--target-directory', str(project), 'doctor', '--dependency-mode', 'prompt-only'],
            capture_output=True, text=True, timeout=180,
        env={**os.environ, 'HOME': str(home)},
        )
        (root / f'doctor-{phase}.json').write_text(result.stdout)
        assert result.returncode == 0, result.stderr or result.stdout[-2000:]
        doctor = json.loads(result.stdout)
        assert doctor.get('ok') is True and not doctor.get('blockers'), doctor.get('blockers')
        print(json.dumps({'phase': phase, 'mode': mode, 'ok': True,
                          'uid': os.getuid(), 'doctor_exit': result.returncode}), flush=True)


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in {'install', 'recreate'}:
        raise SystemExit('usage: container_install_acceptance.py install|recreate')
    run(sys.argv[1])
