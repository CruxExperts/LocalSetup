#!/usr/bin/env python3
"""Prepare reviewed LocalSetup skill registration. Default is a read-only diff."""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import shutil

ROWS = (
    ('ls-public-https-origins', 'ops', 'operations', ['https', 'unix-socket', 'tls'],
     'python3 scripts/public_https.py --help'),
    ('ls-skill-capture-handoff', 'skill-lifecycle', 'skill-lifecycle', ['skills', 'capture', 'handoff'],
     'python3 scripts/package_skill.py --help'),
)


def update_pack(text: str) -> str:
    prefix, separator, remaining = text.partition('\npacks:\n')
    skills, boundary, suffix = remaining.partition('\nworkflow_packs:\n')
    if not separator or not boundary:
        raise ValueError('pack sections changed; review before integration')
    for name, pack, classification, tags, _ in ROWS:
        if name in text:
            raise ValueError('candidate already registered; review existing ownership')
        anchor = '\n  ' + pack + ':\n'
        if skills.count(anchor) != 1:
            raise ValueError('pack contract changed; review before integration')
        skills = skills.replace(anchor, anchor + '  - ' + name + '\n', 1)
        anchor = '\n  skill_taxonomy:\n'
        if suffix.count(anchor) != 1:
            raise ValueError('taxonomy contract changed; review before integration')
        row = '\n    ' + name + ':\n      class: ' + classification
        row += '\n      sort_priority: 60\n      tags:\n'
        row += ''.join('      - ' + tag + '\n' for tag in tags)
        row += '      owner_scope: skill\n'
        suffix = suffix.replace(anchor, anchor.rstrip('\n') + row, 1)
    return prefix + separator + skills + boundary + suffix


def update_smoke(text: str) -> str:
    for name, _, _, _, command in ROWS:
        if name + ':' in text:
            raise ValueError('candidate smoke entry already exists; review ownership')
        text += '\n' + name + ': "' + command + '"\n'
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='Copy packages and edit registration')
    args = parser.parse_args()
    root = args.source_root.resolve(strict=True)
    own = Path(__file__).resolve().parent
    changes = []
    for relative, transform in (
            ('ls/config/pack.yaml', update_pack),
            ('ls/tests/skill_smoke_commands.yaml', update_smoke)):
        path = root / relative
        if path.is_symlink():
            raise ValueError('refusing symlinked registration metadata')
        before = path.read_text()
        after = transform(before)
        changes.append((path, before, after))
        print(''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                         fromfile=relative, tofile=relative)), end='')
    for name, *_ in ROWS:
        if (root / 'ls/skills' / name).exists():
            raise ValueError('candidate skill destination exists; do not overwrite')
        if not (own / name / 'SKILL.md').is_file():
            raise ValueError('complete candidate source is required')
    if args.apply:
        # The maintainer must preserve a clean/checkpointed owning checkout first.
        for name, *_ in ROWS:
            shutil.copytree(own / name, root / 'ls/skills' / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for path, _, after in changes:
            path.write_text(after)
        print('Registration applied. Regenerate both catalogs and validate before integration.')
    else:
        print('Read-only proposal; pass --apply only in the authorized owning checkout.')


if __name__ == '__main__':
    main()
