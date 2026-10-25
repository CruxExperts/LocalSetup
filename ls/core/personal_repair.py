"""Repair recorded personal exposure without changing installation ownership."""
import os
import time
import uuid
from pathlib import Path
from .apply_journal import write_journal, restore_failed_mutations, cleanup_backups
from .locking import package_root_lock
from .manifests import load_pack_config
from .models import PlanAction
from .paths import expand_user_path, global_layout
from .personal_adapter import selection, write
from .personal_inventory import personal_inventory
from .provenance import is_managed_package


def _plan(repo_root: Path, home: Path, clients: list[str] | None, *, allow_source_drift=False):
    inventory = personal_inventory(repo_root, home, clients, allow_source_drift=allow_source_drift)
    blockers = [issue for issue in inventory["issues"] if issue.startswith("invalid personal")]
    owners = inventory["owners"]
    from .mutable_ownership import require_owned_copies
    try:require_owned_copies(repo_root, home, [p for row in owners for p in row["paths"]])
    except ValueError as exc:blockers.append(str(exc))
    if clients is not None:
        recorded = {row["owner"]["client"] for row in owners}
        blockers.extend(f"no recorded personal owner: {client}" for client in sorted(set(clients) - recorded))
    global_root = expand_user_path(load_pack_config(repo_root).global_root, home)
    broken = {row["path"] for row in inventory["adapters"] if not row["ok"]}
    actions = {}
    for row in owners:
        for path in row["paths"]:
            if path not in broken:continue
            action = actions.setdefault(path, PlanAction("attach_personal_path", Path(path), {
                "owners": [], "platforms": [], "packages": [], "mode": row["mode"], "global_root": str(global_root)}))
            if action.details["mode"] != row["mode"]:blockers.append(f"conflicting recorded modes: {path}")
            action.details["owners"].append(row["owner"])
            action.details["platforms"].append(row["owner"]["client"])
            action.details["packages"] = sorted(set(action.details["packages"]) | set(row["packages"]))
            from .package_bindings import merge_bindings
            action.details["package_bindings"] = merge_bindings(
                action.details.get("package_bindings", {}), row.get("package_bindings", {}))
    try:
        from .adapter_alias_reconciliation import plan_alias_retirements

        retirements, conflicts = plan_alias_retirements(
            repo_root, home, repo_root, list(actions.values())
        )
        by_path: dict[str, list[str]] = {}
        for row in retirements:
            by_path.setdefault(str(Path(row["path"]).parent.resolve(strict=False)), []).append(row["alias"])
        for action in actions.values():
            action.details["legacy_alias_retirements"] = sorted(
                set(by_path.get(str(action.path.resolve(strict=False)), []))
            )
        blockers.extend(
            f"{row['path']}: cannot retire package alias safely: {row['reason']}"
            for row in conflicts
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        blockers.append(f"could not verify legacy package alias ownership: {exc}")
    for action in actions.values():
        try:
            for name in selection(repo_root, home, action, allow_source_drift=allow_source_drift):
                from .package_bindings import package_source, verify_binding
                bindings = action.details.get("package_bindings", {})
                if name in bindings:
                    verify_binding(global_root, bindings[name], allow_stale_contract=allow_source_drift)
                package = package_source(global_root, name, bindings=bindings)
                if not package.is_dir() or not is_managed_package(package):
                    raise ValueError(f"reinstall missing managed library package: {name}")
        except (ValueError, OSError) as exc:blockers.append(str(exc))
    from .hermes_adapter import hermes_adapter_blockers
    blockers.extend(b["reason"] for b in hermes_adapter_blockers(repo_root, list(actions.values()), home, repo_root))
    from .claude_prerequisite import claude_prerequisite_blockers
    blockers.extend(b["reason"] for b in claude_prerequisite_blockers(repo_root, list(actions.values()), home, repo_root))
    from .gemini_prerequisite import gemini_prerequisite_blockers
    blockers.extend(b["reason"] for b in gemini_prerequisite_blockers(repo_root, list(actions.values()), home, repo_root))
    from .openclaw_prerequisite import openclaw_prerequisite_blockers
    blockers.extend(b["reason"] for b in openclaw_prerequisite_blockers(repo_root, list(actions.values()), home, repo_root))
    from .kimi_prerequisite import kimi_prerequisite_blockers
    blockers.extend(b["reason"] for b in kimi_prerequisite_blockers(repo_root, list(actions.values()), home, repo_root))
    from .factory_preflight import factory_skill_blockers
    blockers.extend(b["reason"] for b in factory_skill_blockers(repo_root, list(actions.values()), home, repo_root))
    from .amp_preflight import amp_skill_blockers
    blockers.extend(b["reason"] for b in amp_skill_blockers(repo_root, list(actions.values()), home, repo_root))
    from .opencode_preflight import opencode_skill_blockers
    blockers.extend(b["reason"] for b in opencode_skill_blockers(repo_root, list(actions.values()), home, repo_root))
    from .goose_prerequisite import goose_prerequisite_blockers
    blockers.extend(b["reason"] for b in goose_prerequisite_blockers(repo_root, list(actions.values()), home, repo_root))
    payload = {"schema_version": 1, "ok": not blockers, "applied": False,
               "actions": [{"kind": a.kind, "path": str(a.path), "details": a.details} for a in actions.values()],
               "blockers": blockers, "verification": inventory}
    return payload, list(actions.values())


def repair_personal(repo_root: Path, home: Path, clients: list[str] | None = None, *, apply: bool = False) -> dict:
    payload, actions = _plan(repo_root, home, clients)
    if not apply or not payload["ok"] or not actions:return payload
    state_root = global_layout(home).localsetup_home
    with package_root_lock(state_root):
        payload, actions = _plan(repo_root, home, clients)
        if not payload["ok"] or not actions:return payload
        path = state_root / "state/personal-repair" / f"{int(time.time())}-{uuid.uuid4().hex}.json"
        journal = {"version": 1, "status": "started", "touched": []}
        write_journal(path, journal)
        try:
            for action in actions:write(repo_root, home, action, journal, path, all_actions=actions)
            verified = personal_inventory(repo_root, home, clients)
            if not verified["ok"]:raise ValueError("personal repair verification failed")
            journal["status"] = "committed";write_journal(path, journal)
        except Exception as exc:
            journal["status"] = "failed"
            try:restore_failed_mutations(journal, os.replace)
            except Exception as recovery:journal["rollback_error"] = str(recovery)
            write_journal(path, journal)
            return payload | {"ok": False, "blockers": [str(exc)], "journal": str(path),
                              "recovery_ok": "rollback_error" not in journal}
        cleanup_backups(journal)
        return payload | {"ok": True, "applied": True, "verification": verified, "journal": str(path)}
