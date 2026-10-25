"""Bounded static collision checks for Amp's documented local skill roots."""
import os
import stat
from pathlib import Path

import yaml


def _name(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('skill metadata is not a regular file')
        raw = os.read(fd, 16384).decode('utf-8')
    finally:os.close(fd)
    if not raw.startswith('---\n') or '\n---\n' not in raw[4:]:
        raise ValueError('skill frontmatter is absent or exceeds 16 KiB')
    node = yaml.compose(raw[4:raw.index('\n---\n', 4)])
    if not isinstance(node, yaml.MappingNode):raise ValueError('skill frontmatter is not a mapping')
    names = [value for key, value in node.value if isinstance(key, yaml.ScalarNode) and key.value == 'name']
    if len(names) != 1 or not isinstance(names[0], yaml.ScalarNode) or names[0].tag != 'tag:yaml.org,2002:str' or not names[0].value:
        raise ValueError('skill frontmatter needs one string name')
    return names[0].value


def _roots(home: Path, target: Path):
    roots = [home / '.config/agents/skills', home / '.agents/skills', home / '.config/amp/skills']
    ancestors = [target, *target.parents]
    if home in ancestors:ancestors = ancestors[:ancestors.index(home) + 1]
    for relative in ('.agents/skills', '.claude/skills'):
        roots.extend(parent / relative for parent in ancestors)
    roots.append(home / '.claude/skills')
    return list(dict.fromkeys(roots))


def amp_skill_blockers(source: Path, actions, home: Path, target: Path) -> list[dict]:
    from .amp_ownership import records, affected_actions, portable_counterpart
    def blocked(path, reason):
        return {'path': str(path), 'status_code': 'amp_skill_precedence_conflict', 'reason': reason}
    try:
        recorded, library = records(source, home)
        extra, other_targets = affected_actions(actions, recorded, library)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return [blocked(target, 'Amp affected ownership could not be established; repair registry metadata before changing shared content')]
    adapters = [a for a in [*actions, *extra] if a.kind in {'attach_repo_path', 'attach_personal_path', 'repair_repo_path'}]
    if not any('amp-cli' in a.details.get('platforms', []) for a in adapters):return []
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if xdg and Path(xdg).expanduser().absolute() != home.absolute() / '.config':
        return [blocked(home, 'Amp loose-skill roots with nondefault XDG_CONFIG_HOME are unqualified; do not assume plugin path rules apply')]
    planned = {a.path.absolute(): set(a.details.get('packages', [])) for a in adapters if not a.details.get('_amp_recorded_only')}
    desired = {};canonical = {};expected = {}
    try:
        for action in adapters:
            for package in action.details.get('packages', []):
                from .amp_ownership import selected_package_paths
                library, path = selected_package_paths(source, actions, action, package)
                name = _name(path / 'SKILL.md')
                desired[name] = package;canonical[name] = library.resolve(strict=False);expected[name] = path
        count = 0;blockers = []
        roots = list(dict.fromkeys(root for scope in [target, *other_targets] for root in _roots(home.absolute(), scope.absolute())))
        for root in roots:
            if root.is_symlink():raise ValueError(f'unqualified symlink skill root: {root}')
            if not root.exists():continue
            if not root.is_dir():raise ValueError(f'skill root is not a directory: {root}')
            with os.scandir(root) as entries:
                for entry in entries:
                    count += 1
                    if count > 4096:raise ValueError('local skill scan exceeds 4096 entries')
                    path = Path(entry.path);metadata = path / 'SKILL.md'
                    if not entry.is_dir():
                        if entry.is_symlink():raise ValueError(f'unresolved skill entry: {path}')
                        continue
                    if not metadata.exists() and not metadata.is_symlink():continue
                    name = _name(metadata)
                    if name not in desired:continue
                    package = desired[name]
                    if path.name == package and package in planned.get(root, set()):continue
                    if path.is_symlink() and path.resolve(strict=False) == canonical[name]:continue
                    if portable_counterpart(path, canonical[name], expected[name], recorded):continue
                    blockers.append(blocked(path, 'Amp frontmatter name conflicts with a planned skill across documented local roots; preserve this origin and resolve selection explicitly'))
        return blockers
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        # Do not copy YAML snippets or file contents into diagnostic output.
        return [blocked(target, f'Amp local skill precedence could not be established ({type(exc).__name__}); inspect the documented roots without changing content')]
