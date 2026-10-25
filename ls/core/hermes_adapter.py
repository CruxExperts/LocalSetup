"""Preflight the explicitly bound Hermes native mutable-copy adapter."""
import os
from pathlib import Path

from .mutable_adapters import check_existing
from .mutable_packages import capture_baselines

CLIENT = 'hermes-agent'


def hermes_adapter_blockers(source: Path, actions, home: Path, target: Path) -> list[dict]:
    """Validate writes without loading Hermes, resolving credentials or changing trust.

    This adapter binds the default personal profile, not an ambient CLI/API
    session. Repository discovery remains conditional on native Git-root trust.
    Existing recorded copies retain their mutable designation at the writer.
    """
    blockers = []
    for action in actions:
        clients = set(action.details.get('platforms', []))
        clients.update(o.get('client') for o in action.details.get('owners', []))
        if action.details.get('platform'):clients.add(action.details['platform'])
        if CLIENT not in clients or action.kind not in {'attach_repo_path', 'attach_personal_path', 'repair_repo_path'}:continue
        try:
            personal = action.kind == 'attach_personal_path'
            expected = (home if personal else target) / '.hermes/skills'
            if action.path.absolute() != expected.absolute():
                raise ValueError('Hermes adapter path differs from its explicit default-profile binding')
            if action.details.get('mode') != 'portable':
                raise ValueError('Hermes requires portable independent copies; select --mode portable')
            if personal:
                override = os.environ.get('HERMES_HOME')
                if override and Path(override).expanduser().absolute() != (home / '.hermes').absolute():
                    raise ValueError('Nondefault HERMES_HOME requires separate profile qualification; default-profile writes refused')
            if not action.details.get('mutable_copy') and check_existing(action.path) is None:
                raise ValueError('Hermes writes require an explicit mutable-copy designation')
            names = set(action.details.get('packages', []))
            for selection in action.details.get('owner_packages', {}).values():names.update(selection)
            # Inspect authored inputs before install_skills/install_workflows can
            # follow a resource link while populating the canonical library.
            for name in sorted(names):
                binding = action.details.get('package_bindings', {}).get(name)
                if binding:
                    from .skills_external import validate_source_binding
                    custom = validate_source_binding(binding)
                    capture_baselines(custom.path.parent, [name])
                    continue
                parent = source / 'ls/skills'
                if not (parent / name).exists():parent = source / 'ls/workflows'
                capture_baselines(parent, [name])
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            blockers.append({'path': str(action.path), 'status_code': 'hermes_adapter_preservation', 'reason': str(exc)})
    return blockers
