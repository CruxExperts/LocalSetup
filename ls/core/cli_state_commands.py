from __future__ import annotations

import re

from .client_registry import (
    ClientRegistry,
    ClientRegistryError,
    ClientVariant,
    compare_variants,
    load_client_registry,
    platform_rows,
    projection_matches,
    write_platforms_projection,
)
from .cli_handler_sync import sync


_CANONICAL_CLIENT_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")


def _resolve_variant(registry: ClientRegistry, key: str) -> ClientVariant:
    if not isinstance(key, str) or _CANONICAL_CLIENT_KEY.fullmatch(key) is None:
        raise ClientRegistryError(
            f"invalid client registry key {key!r}: expected lowercase family/variant in kebab-case"
        )
    family_id, variant_id = key.split("/", 1)
    try:
        return registry.variant(family_id, variant_id)
    except KeyError as exc:
        raise ClientRegistryError(f"unknown client registry variant: {key}") from exc


def _adapter_check_commands(platform_ids: list[str] | None, *, has_issues: bool) -> list[dict]:
    selector = f" --tools {','.join(platform_ids)}" if platform_ids else ""
    commands = [
        {
            "command": f"localsetup verify{selector}",
            "reason": "run the full filesystem verification report",
        },
        {
            "command": f"localsetup doctor{selector}",
            "reason": "inspect target health and repair planning context",
        },
    ]
    if has_issues:
        commands.extend(
            [
                {
                    "command": f"localsetup doctor repair{selector}",
                    "reason": "plan or apply existing LocalSetup repair behavior after review",
                },
                {
                    "command": f"localsetup install{selector}",
                    "reason": "refresh selected adapter attachments using the existing installer",
                },
            ]
        )
    return commands


def _adapter_check_payload(root, home, *, target_root, platform_ids, include_provenance: bool) -> dict:
    verify = verify_install(
        root,
        home=home,
        platform_ids=platform_ids,
        target_root=target_root,
        level="filesystem",
    )
    adapters = verify["adapters"]
    platforms = sorted({str(adapter.get("platform")) for adapter in adapters if adapter.get("platform")})
    managed_packages = sorted(
        {
            str(package)
            for adapter in adapters
            for package in adapter.get("managed_visible_packages", adapter.get("visible_packages", []))
            if package
        }
    )
    visible_packages = sorted(
        {
            str(package)
            for adapter in adapters
            for package in adapter.get("visible_packages", [])
            if package
        }
    )
    issues = list(verify.get("issues", []))
    warnings = sorted(set([*verify.get("warnings", []), *verify.get("provenance_warnings", [])]))
    repair_hints = sorted(
        set(
            [
                *verify.get("provenance_repair_hints", []),
                *verify.get("tmux_terminal_mode_repair_hints", []),
            ]
        )
    )
    if issues:
        repair_hints.extend(
            [
                "run localsetup verify for the full rule report",
                "run localsetup doctor before applying repair commands",
            ]
        )
        repair_hints = sorted(set(repair_hints))

    payload = {
        "ok": bool(verify.get("ok")),
        "adapters": adapters,
        "issues": issues,
        "warnings": warnings,
        "repair_hints": repair_hints,
        "summary": {
            "adapter_count": len(adapters),
            "platforms": platforms,
            "managed_packages": managed_packages,
            "visible_packages": visible_packages,
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "commands": _adapter_check_commands(platform_ids, has_issues=bool(issues)),
        "rules": verify.get("rules", []),
    }
    if include_provenance:
        payload["provenance"] = verify.get("provenance", {})
    return payload


def handle(cli, args, root, home) -> int | None:
    sync(globals(), cli)

    if args.cmd == "client-registry":
        registry = load_client_registry(root)
        if args.client_registry_action == "drift":
            payload = compare_variants(_resolve_variant(registry, args.left), _resolve_variant(registry, args.right))
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        matches = projection_matches(root, registry)
        if args.client_registry_action == "generate":
            write_platforms_projection(root, registry)
            matches = True
        payload = {
            "ok": matches,
            "action": args.client_registry_action,
            "families": len(registry.families),
            "variants": len(registry.variants()),
            "platforms": [row["id"] for row in platform_rows(registry)],
            "projection": "ls/config/platforms.yaml",
            "projection_matches": matches,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if matches else 1

    if args.cmd == "verify":
        started_at = time.time()
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        attachment_root = target_root or root
        git_pre = git_status_snapshot(attachment_root)
        payload = verify_install(root, home=home, platform_ids=config.platforms, target_root=target_root, level=args.level)
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=attachment_root,
            operation="verify",
            mode=args.level,
            payload=payload,
            git_pre=git_pre,
            git_post=git_status_snapshot(attachment_root),
            planned_paths=[".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
        )
        _write_report(config.output.report, payload)
        _print_payload(payload)
        write_trace(getattr(args, "trace_json", None), event="verify", status="ok" if payload["ok"] else "failed", attributes={"level": args.level}, started_at=started_at)
        return 0 if payload["ok"] else 1

    if args.cmd == "rollback":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        payload = rollback(root, home=home, platform_ids=config.platforms, target_root=target_root)
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0

    if args.cmd == "adapters":
        pack = load_pack_config(root)
        target_root = Path(getattr(args, "target_directory", None)).expanduser().resolve() if getattr(args, "target_directory", None) else None
        global_root = expand_user_path(pack.global_root, home)
        platform_ids = _split_csv(args.platforms)
        if getattr(args, "adapter_action", None) == "check":
            payload = _adapter_check_payload(
                root,
                home,
                target_root=target_root,
                platform_ids=platform_ids,
                include_provenance=bool(args.provenance),
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload["ok"] else 1
        payload = adapter_status(
            root,
            home,
            global_root,
            platform_ids=platform_ids,
            target_root=target_root,
        )
        if args.provenance:
            attachment_root = target_root or root
            registry_path = expand_user_path(pack.global_registry, home)
            lock = json.loads((attachment_root / ".localsetup" / "lock.json").read_text(encoding="utf-8")) if (attachment_root / ".localsetup" / "lock.json").exists() else {}
            provenance = provenance_report(
                root,
                lock=lock,
                registry=load_registry(registry_path) if registry_path.exists() else {},
                global_root=global_root,
                adapters=payload,
            )
            print(json.dumps({"adapters": payload, "provenance": provenance, "provenance_warnings": provenance["warnings"], "provenance_repair_hints": provenance["repair_hints"]}, indent=2, sort_keys=True))
            return 0
        print(
            json.dumps(
                payload,
                indent=2,
            )
        )
        return 0

    if args.cmd == "configure":
        config = _resolved_config(args, home)
        payload = config_to_dict(config)
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0

    if args.cmd == "doctor":
        started_at = time.time()
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        if getattr(args, "doctor_action", None) == "repair":
            if getattr(args, "envman", False):
                _print_payload(
                    {"tool": "envman", "status": "not-started", "code": "doctor-repair-not-supported"}
                )
                return 2
            attachment_root = target_root or root
            git_pre = git_status_snapshot(attachment_root)
            payload = run_repair(
                root,
                home=home,
                target_root=target_root,
                platform_ids=config.platforms,
                backup_dir=Path(config.backup_dir).expanduser().resolve() if config.backup_dir else None,
                dependency_mode=config.dependency_mode,
                apply=bool(args.apply),
                repair_mode=getattr(args, "repair_mode", None) or ("safe-repair" if args.apply else "report-only"),
                allow=getattr(args, "allow", None) or [],
            )
            emit_prompt = getattr(args, "emit_agent_prompt", None)
            if getattr(args, "agent_prompt", False) or emit_prompt:
                prompt_path = Path(emit_prompt).expanduser().resolve() if emit_prompt else None
                payload["agent_prompt"] = agent_prompt_payload(payload, path=prompt_path)
            _record_health_for_payload(
                root=root,
                home=home,
                target_root=attachment_root,
                operation="doctor.repair",
                mode=payload.get("repair_mode", "report-only"),
                payload=payload,
                git_pre=git_pre,
                git_post=git_status_snapshot(attachment_root),
                planned_paths=[".localsetup/lock.json", ".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
            )
            _write_report(config.output.report, payload)
            _print_payload(payload)
            write_trace(
                getattr(args, "trace_json", None),
                event="doctor.repair",
                status="ok" if payload["ok"] else "failed",
                attributes={"target_root": str(target_root or root), "applied": bool(payload["applied"])},
                started_at=started_at,
            )
            return 0 if payload["ok"] else 1
        payload = run_doctor(
            root,
            home=home,
            packs=config.packs,
            platform_ids=config.platforms,
            dependency_mode=config.dependency_mode,
            data_root=_config_data_root(config, home),
            target_root=target_root,
            envman=bool(getattr(args, "envman", False)),
        )
        payload["shell_registration"] = shell_registration_status(root, home=home)
        if payload["shell_registration"]["warnings"]:
            payload["warnings"].extend(payload["shell_registration"]["warnings"])
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=target_root or root,
            operation="doctor",
            mode="report-only",
            payload=payload,
            git_pre=git_status_snapshot(target_root or root),
            git_post=git_status_snapshot(target_root or root),
            planned_paths=[".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
        )
        _write_report(config.output.report, payload)
        _print_payload(payload)
        write_trace(getattr(args, "trace_json", None), event="doctor", status="ok" if payload["ok"] else "failed", attributes={"target_root": str(target_root or root)}, started_at=started_at)
        return 0 if payload["ok"] else 1

    if args.cmd == "provenance":
        if args.provenance_action == "repair":
            payload = {
                "ok": True,
                "planned": True,
                "actions": [],
                "message": "provenance repair is intentionally report-only in this release",
            }
            _print_payload(payload)
            return 0
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        attachment_root = target_root or root
        pack = load_pack_config(root)
        global_root = expand_user_path(pack.global_root, home)
        registry_path = expand_user_path(pack.global_registry, home)
        lock_path = attachment_root / ".localsetup" / "lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
        adapters = (
            adapter_status(root, home, global_root, platform_ids=config.platforms, target_root=target_root)
            if config.platforms is not None
            else recorded_adapter_status(lock, global_root)
        )
        payload = provenance_report(
            root,
            lock=lock,
            registry=load_registry(registry_path) if registry_path.exists() else {},
            global_root=global_root,
            adapters=adapters,
        )
        payload["adapters"] = adapters
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0

    if args.cmd == "health":
        target_root = Path(getattr(args, "target_directory", None)).expanduser().resolve() if getattr(args, "target_directory", None) else root
        if getattr(args, "health_action", None) == "repair-queue":
            output_dir = getattr(args, "agent_prompts", None)
            payload = (
                write_repair_queue_prompts(home=home, output_dir=Path(output_dir).expanduser().resolve())
                if output_dir
                else repair_queue(home=home)
            )
            _print_payload(payload)
            return 0
        _print_payload(read_health_status(home=home, target_root=target_root))
        return 0

    if args.cmd == "migrate":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        attachment_root = target_root or root
        git_pre = git_status_snapshot(attachment_root)
        apply_migration = not args.dry_run and config.migration_mode != "report-only"
        payload = conservative_migrate(
            root,
            home=home,
            platform_ids=config.platforms,
            target_root=target_root,
            backup_dir=Path(config.backup_dir).expanduser().resolve() if config.backup_dir else None,
            apply=apply_migration,
        )
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=attachment_root,
            operation="migrate",
            mode="apply" if apply_migration else "report-only",
            payload=payload,
            git_pre=git_pre,
            git_post=git_status_snapshot(attachment_root),
            planned_paths=[".localsetup/lock.json", ".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
        )
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "convert":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        attachment_root = target_root or root
        git_pre = git_status_snapshot(attachment_root)
        payload = convert_repo(
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
            platform_ids=config.platforms,
            attach_mode=config.attach_mode,
            target_root=target_root,
            backup_dir=Path(config.backup_dir).expanduser().resolve() if config.backup_dir else None,
            dependency_mode=config.dependency_mode,
            apply=bool(args.apply),
        )
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=attachment_root,
            operation="convert",
            mode="apply" if args.apply else "report-only",
            payload=payload,
            git_pre=git_pre,
            git_post=git_status_snapshot(attachment_root),
            planned_paths=[".localsetup/lock.json", ".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
        )
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "context":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        payload = build_agent_context(root, home=home, config=config)
        markdown = bool(config.output.markdown)
        _write_report(config.output.report, payload, markdown=markdown)
        _print_payload(payload, markdown=markdown)
        return 0 if not payload["blockers"] else 1

    if args.cmd == "generate-docs":
        print(json.dumps(generate_alias_outputs(root), indent=2))
        return 0

    return None
