from __future__ import annotations

from .cli_handler_sync import sync

def handle(cli, args, root, home) -> int | None:
    sync(globals(), cli)

    if args.cmd in {"plan", "install", "update"}:
        started_at = time.time()
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        attachment_root = target_root or root
        from .installation_ownership import validate_scope_request, resolve_skill_scope
        recorded = validate_scope_request(attachment_root, None)
        if (recorded and config.skill_scope in {"repo", "personal"}
                and resolve_skill_scope(attachment_root, None) == "both"):
            from .recorded_mode import requested_mode
            if not _selector_free(config) or requested_mode(args) is not None:
                raise ValueError("Retire recorded scope separately from reselection or mode changes")
            if config.skill_scope == "personal":
                from .scope_retirement import retire_repository_scope as retire_scope
            else:
                from .personal_scope_retirement import retire_personal_scope as retire_scope
            payload = retire_scope(root, home, attachment_root,
                apply=args.cmd == "update" or (args.cmd == "install" and args.apply))
            _write_report(config.output.report, payload)
            _print_payload(payload)
            return 0 if payload["ok"] else 2
        additive = (recorded and config.skill_scope == "both"
                    and resolve_skill_scope(attachment_root, None) in {"repo", "personal"})
        if additive:
            if not _selector_free(config):
                raise ValueError("Migrate recorded scope separately from client or package reselection")
        else:
            validate_scope_request(attachment_root, config.skill_scope)
        if (recorded and resolve_skill_scope(attachment_root, None) == "personal"
                and config.platforms is None and not _selector_free(config)):
            raise ValueError("Select clients explicitly when changing recorded personal package selections")
        if additive:
            from .scope_migration import build_additive_scope_plan
            auto_context = {"mode": "additive_scope", "repair": {},
                            "plan": build_additive_scope_plan(root, home, attachment_root)}
        else:
            auto_context = (
                _auto_default_context(root, home, config, attachment_root)
                if _selector_free(config) and (config.target_directory or recorded) and (config.skill_scope is None or recorded)
                else None
            )
        if auto_context is not None:
            mode = str(auto_context["mode"])
            repair = auto_context["repair"]
            plan = auto_context["plan"]
            if plan is not None:
                plan.codex_agent_conflict = getattr(args, "codex_agent_conflict", "error")
                if mode in {"recorded_repo", "recorded_personal", "recorded_both", "inferred_existing", "additive_scope"}:
                    from .recorded_mode import requested_mode, set_recorded_mode
                    set_recorded_mode(root, home, attachment_root, plan, requested_mode(args))
                policy = _policy_findings(root, plan.rollback_metadata.get("skills", []), getattr(args, "policy_mode", "standard"))
                if args.cmd == "plan" or (args.cmd == "install" and not args.apply):
                    from .apply_preflight import preflight_install_plan
                    preflight = preflight_install_plan(root, plan, home, target_root=attachment_root)
                    payload = _auto_plan_payload(root, home, config, attachment_root, plan, policy, mode=mode, repair=repair)
                    payload["preflight"] = preflight
                    _write_report(config.output.report, payload)
                    _print_payload(payload)
                    write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok", attributes={"dry_run": True, "auto_mode": mode}, started_at=started_at)
                    return 0
                result, status = _apply_install_plan(root, home, config, attachment_root, plan, policy, mode=mode)
                result["repair"] = repair
                _write_report(config.output.report, result)
                _print_payload(result)
                write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok" if status == 0 else "failed", attributes={"target_root": str(attachment_root), "auto_mode": mode}, started_at=started_at)
                return status

            policy = _policy_findings(
                root,
                repair.get("inferred", {}).get("repo_skills", []),
                getattr(args, "policy_mode", "standard"),
            )
            if args.cmd == "plan" or (args.cmd == "install" and not args.apply):
                payload = {
                    "auto_mode": mode,
                    "ok": bool(repair.get("ok")) and not bool(policy.get("blockers")),
                    "config": config_to_dict(config),
                    "attachment": {
                        "target_root": str(attachment_root),
                        "platforms": repair.get("inferred", {}).get("platforms", []),
                        "global_only": not bool(repair.get("inferred", {}).get("platforms", [])),
                    },
                    "repair": repair,
                    "policy": policy,
                    "actions": repair.get("actions", []),
                    "warnings": [*repair.get("warnings", []), *policy.get("warnings", [])],
                    "decisions": repair.get("decisions", []),
                    "blockers": [*repair.get("blockers", []), *policy.get("blockers", [])],
                }
                _write_report(config.output.report, payload)
                _print_payload(payload)
                write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok" if payload["ok"] else "failed", attributes={"dry_run": True, "auto_mode": mode}, started_at=started_at)
                return 0

            if policy["blockers"]:
                payload = {
                    "auto_mode": mode,
                    "ok": False,
                    "attachment": {
                        "target_root": str(attachment_root),
                        "platforms": repair.get("inferred", {}).get("platforms", []),
                        "global_only": not bool(repair.get("inferred", {}).get("platforms", [])),
                    },
                    "repair": repair,
                    "policy": policy,
                    "warnings": [*repair.get("warnings", []), *policy.get("warnings", [])],
                    "decisions": repair.get("decisions", []),
                    "blockers": [*repair.get("blockers", []), *policy["blockers"]],
                }
                _write_report(config.output.report, payload)
                _print_payload(payload)
                write_trace(getattr(args, "trace_json", None), event=args.cmd, status="failed", attributes={"target_root": str(attachment_root), "auto_mode": mode}, started_at=started_at)
                return 1

            applied = run_repair(
                root,
                home=home,
                target_root=attachment_root,
                platform_ids=None,
                backup_dir=Path(config.backup_dir).expanduser().resolve() if config.backup_dir else None,
                dependency_mode=config.dependency_mode,
                apply=True,
            )
            applied["auto_mode"] = mode
            _write_report(config.output.report, applied)
            _print_payload(applied)
            write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok" if applied["ok"] else "failed", attributes={"target_root": str(attachment_root), "auto_mode": mode, "applied": bool(applied.get("applied"))}, started_at=started_at)
            return 0 if applied["ok"] else 1

        plan = build_install_plan(
            root,
            home=home,
            packs=config.packs,
            preset=config.preset,
            skills=config.skills,
            workflows=config.workflows,
            skill_classes=config.skill_classes,
            skill_tags=config.skill_tags,
            exclude_skills=config.exclude_skills,
            global_packs=config.global_packs,
            global_preset=config.global_preset,
            global_skills=config.global_skills,
            global_workflows=config.global_workflows,
            global_skill_classes=config.global_skill_classes,
            global_skill_tags=config.global_skill_tags,
            global_exclude_skills=config.global_exclude_skills,
            repo_packs=config.repo_packs,
            repo_preset=config.repo_preset,
            repo_skills=config.repo_skills,
            repo_workflows=config.repo_workflows,
            repo_skill_classes=config.repo_skill_classes,
            repo_skill_tags=config.repo_skill_tags,
            repo_exclude_skills=config.repo_exclude_skills,
            skill_scope=config.skill_scope,
            attach_mode=config.attach_mode,
            platform_ids=config.platforms,
            target_root=target_root,
        )
        plan.codex_agent_conflict = getattr(args, "codex_agent_conflict", "error")
        policy = _policy_findings(root, plan.rollback_metadata.get("skills", []), getattr(args, "policy_mode", "standard"))
        detected_target = bool(getattr(args, "detected_target_directory", False))
        if args.cmd == "plan" or (args.cmd == "install" and not args.apply):
            from .apply_preflight import preflight_install_plan
            preflight = preflight_install_plan(root, plan, home, target_root=attachment_root)
            warnings = []
            if config.target_directory and not config.platforms and not detected_target:
                warnings.append("target directory was provided but no platforms were selected; plan is global-only with no repo adapters")
            payload = {
                "auto_mode": "explicit",
                "actions": [{"kind": a.kind, "path": str(a.path), "details": a.details} for a in plan.actions],
                "config": config_to_dict(config),
                "attachment": {
                    "target_root": str(attachment_root),
                    "platforms": plan.rollback_metadata.get("platforms", []),
                    "global_only": plan.rollback_metadata.get("global_only", False),
                },
                "inventory": install_inventory(root, home=home, target_root=target_root, platform_ids=config.platforms),
                "warnings": warnings,
                "policy": policy,
                "rollback": plan.rollback_metadata,
                "preflight": preflight,
            }
            _write_report(config.output.report, payload)
            _print_payload(payload)
            write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok", attributes={"dry_run": True}, started_at=started_at)
            return 0
        result, status = _apply_install_plan(root, home, config, attachment_root, plan, policy)
        result["auto_mode"] = "explicit"
        if config.target_directory and not config.platforms and not detected_target:
            result["warnings"] = ["target directory was provided but no platforms were selected; install was global-only with no repo adapters"]
        _write_report(config.output.report, result)
        _print_payload(result)
        write_trace(getattr(args, "trace_json", None), event=args.cmd, status="ok" if status == 0 else "failed", attributes={"target_root": str(attachment_root)}, started_at=started_at)
        return status

    if args.cmd == "wizard":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        if args.repo_profile:
            if not target_root:
                print("localsetup: wizard --repo-profile requires --target-directory", file=sys.stderr)
                return 2
            if args.apply and args.dry_run:
                print("localsetup: use either --apply or --dry-run with --repo-profile, not both", file=sys.stderr)
                return 2
            payload = render_repo_profile(args.repo_profile, target_root, apply=bool(args.apply))
            _write_report(config.output.report, payload)
            _print_payload(payload)
            return 0 if payload.get("ok") else 1
        caller_directory = Path(args.caller_directory).expanduser().resolve() if args.caller_directory else None
        return run_wizard(
            repo_root=root,
            home=home,
            caller_directory=caller_directory,
            target_directory=target_root,
            target_directory_is_explicit=bool(target_root and args.target_directory_origin == "explicit"),
            platforms=config.platforms,
            packs=config.packs,
            preset=config.preset,
            skills=config.skills,
            workflows=config.workflows,
            skill_classes=config.skill_classes,
            skill_tags=config.skill_tags,
            exclude_skills=config.exclude_skills,
            global_packs=config.global_packs,
            global_preset=config.global_preset,
            global_skills=config.global_skills,
            global_workflows=config.global_workflows,
            global_skill_classes=config.global_skill_classes,
            global_skill_tags=config.global_skill_tags,
            global_exclude_skills=config.global_exclude_skills,
            repo_packs=config.repo_packs,
            repo_preset=config.repo_preset,
            repo_skills=config.repo_skills,
            repo_workflows=config.repo_workflows,
            repo_skill_classes=config.repo_skill_classes,
            repo_skill_tags=config.repo_skill_tags,
            repo_exclude_skills=config.repo_exclude_skills,
            attach_mode=config.attach_mode,
            dependency_mode=config.dependency_mode,
            register_shell=not args.no_register_shell,
            color_mode=args.color,
            glyph_mode=args.glyphs,
        )

    return None
