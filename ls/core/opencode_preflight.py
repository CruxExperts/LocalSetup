"""Static OpenCode root/identity checks; effective native configuration is separate."""
import os
from pathlib import Path

import yaml

from .amp_preflight import _name
from .opencode_collisions import conflicting_sources


def discovery_roots(home: Path, target: Path) -> list[Path]:
    from .client_state import probe_git_context
    existing = target
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    git = probe_git_context(existing)
    boundary = git.root if git else home if target.is_relative_to(home) else target
    parents = [target]
    while parents[-1] != boundary and parents[-1] != parents[-1].parent:
        parents.append(parents[-1].parent)
    roots = [base / rel for base in [home, *parents]
             for rel in ('.agents/skills', '.claude/skills', '.opencode/skill', '.opencode/skills')]
    roots += [home / '.config/opencode' / suffix for suffix in ('skill', 'skills')]
    extra = os.environ.get('OPENCODE_CONFIG_DIR')
    if extra:
        path = Path(extra)
        if not path.is_absolute():raise ValueError('relative configuration directory')
        roots += [path / suffix for suffix in ('skill', 'skills')]
    return list(dict.fromkeys(roots))


def opencode_skill_blockers(source: Path, actions, home: Path, target: Path) -> list[dict]:
    from .amp_ownership import records, affected_actions, portable_counterpart
    def blocked(reason):
        return {'path': str(target), 'status_code': 'opencode_skill_inventory', 'reason': reason}
    try:
        recorded, library = records(source, home)
        extra, targets = affected_actions(actions, recorded, library, client_id='opencode')
        adapters = [a for a in [*actions, *extra]
                    if a.kind in {'attach_repo_path', 'attach_personal_path', 'repair_repo_path'}]
        if not any('opencode' in a.details.get('platforms', []) for a in adapters):return []
        for variable, default in [('OPENCODE_TEST_HOME', home), ('XDG_CONFIG_HOME', home / '.config')]:
            value = os.environ.get(variable)
            if value is not None and (not Path(value).is_absolute() or Path(value).resolve() != default.resolve()):
                return [blocked('OpenCode home/configuration override is outside the qualified static inventory; preserve roots and qualify the selected host')]
        flag = os.environ.get('OPENCODE_DISABLE_EXTERNAL_SKILLS')
        if flag is not None and flag != 'false':
            return [blocked('OpenCode external-skill flag is not the supported unset or literal false setting; native boolean parsing is not inferred')]
        intended = {};packages = {}
        for action in adapters:
            for package in action.details.get('packages', []):
                from .amp_ownership import selected_package_paths
                installed, original = selected_package_paths(source, actions, action, package)
                name = _name(original / 'SKILL.md')
                if name in packages and packages[name][0] != package:raise ValueError('planned duplicate name')
                packages[name] = (package, original)
                intended.setdefault(name, set()).update({installed / 'SKILL.md', action.path / package / 'SKILL.md'})
        for name, (package, original) in packages.items():
            for root, names, mode, clients, owner, bindings in recorded:
                from .package_bindings import package_source
                candidate = root / package
                if package in names and portable_counterpart(candidate, package_source(library, package, bindings=bindings), original, recorded):
                    intended[name].add(candidate / 'SKILL.md')
        roots = list(dict.fromkeys(root for scope in [target, *targets] for root in discovery_roots(home, scope)))
        conflicts = conflicting_sources(roots, intended)
        if conflicts:return [blocked('OpenCode selected skill name has distinct conflicting sources in the checked roots; preserve all origins and resolve selection explicitly')]
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, UnicodeError, yaml.YAMLError):
        return [blocked('OpenCode affected ownership or bounded skill inventory cannot be established; no partial inventory is accepted')]


def opencode_verification_blockers(source, home, target, scope, selected):
    from .amp_ownership import records
    from .models import PlanAction
    if selected is not None and 'opencode' not in selected:return []
    try:
        recorded, library = records(source, home)
        actions = []
        for path, names, mode, clients, owner, bindings in recorded:
            if 'opencode' not in clients:continue
            personal = path in {home / '.agents/skills', home / '.config/opencode/skills'} and owner == home
            if (personal and scope in {'personal', 'both'}) or (owner == target and scope != 'personal'):
                actions.append(PlanAction('repair_repo_path', path, {'platforms': ['opencode'],
                    'packages': sorted(names), 'mode': mode, 'global_root': str(library), 'package_bindings': bindings}))
        return opencode_skill_blockers(source, actions, home, target)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return [{'reason': 'OpenCode recorded inventory cannot be established'}]
