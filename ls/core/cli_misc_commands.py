from __future__ import annotations

from .cli_handler_sync import sync

def handle(cli, args, root, home) -> int | None:
    sync(globals(), cli)

    if args.cmd == "envman":
        from .envman import handle as handle_envman

        return handle_envman(args, root)

    if args.cmd == "plugin":
        if args.plugin_action == "list":
            issues = validate_plugin_pack_manifest(root)
            configs = []
            if not issues:
                configs = [
                    {
                        "id": config.plugin_id,
                        "display_name": config.display_name,
                        "description": config.description,
                        "category": config.category,
                        "source_pack": config.source_pack,
                    }
                    for config in load_plugin_pack_configs(root)
                    if args.platform in config.platforms
                ]
            _print_payload({"ok": not issues, "platform": args.platform, "plugin_packs": configs, "issues": issues})
            return 0 if not issues else 1
        if args.plugin_action == "plan":
            payload = plan_plugin_packs(root, args.plugin_packs, platform=args.platform)
            if args.output:
                payload["output"] = str(Path(args.output).expanduser())
            _print_payload(payload)
            return 0
        if args.plugin_action == "build":
            payload = build_codex_plugins(root, Path(args.output).expanduser(), args.plugin_packs)
            _print_payload(payload)
            return 0 if payload.get("ok") else 1
        if args.plugin_action == "validate":
            payload = validate_codex_plugin_path(Path(args.path).expanduser())
            _print_payload(payload)
            return 0 if payload.get("ok") else 1
        print(f"localsetup: unsupported plugin action: {args.plugin_action}", file=sys.stderr)
        return 2

    if args.cmd == "harness":
        if args.harness_topic == "repo-finalizer":
            target_root = _harness_target(args)
            if args.harness_action == "plan":
                payload = repo_finalizer_plan(root, target_root, mode=getattr(args, "mode", None))
            elif args.harness_action == "status":
                payload = repo_finalizer_status(root, target_root, mode=getattr(args, "mode", None))
            elif args.harness_action == "run":
                payload = repo_finalizer_run(
                    root,
                    target_root,
                    mode=getattr(args, "mode", None),
                    no_commit=args.no_commit,
                    checkpoint=args.checkpoint,
                    message=args.message,
                )
            else:
                print(f"localsetup: unsupported repo-finalizer action: {args.harness_action}", file=sys.stderr)
                return 2
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(repo_finalizer_payload_to_text(payload), end="")
            return 0 if payload.get("ok", True) else 1
        if args.harness_topic != "codex-heartbeat":
            print(f"localsetup: unsupported harness topic: {args.harness_topic}", file=sys.stderr)
            return 2
        target_root = _harness_target(args)
        if args.harness_action == "accounting":
            from .agent.heartbeat_accounting_cli import main
            return main(args, target_root or root)
        if args.harness_action == "plan":
            payload = harness_plan(root, target_root)
        elif args.harness_action == "init":
            payload = harness_init(root, target_root)
        elif args.harness_action == "enable":
            payload = harness_enable(root, target_root, install_crontab=args.install_crontab, yes=args.yes)
        elif args.harness_action == "disable":
            payload = harness_disable(root, target_root, install_crontab=args.install_crontab, yes=args.yes)
        elif args.harness_action == "status":
            payload = harness_status(root, target_root)
        elif args.harness_action == "budget":
            payload = harness_budget(root, target_root)
            if args.accounting_root is not None:
                from .agent.heartbeat_accounting_cli import report
                payload["execution_accounting"] = report(args.accounting_root, target_root or root)
        elif args.harness_action == "run":
            from .agent.heartbeat_execution_cli import selected, main
            if selected(args):
                return main(args, target_root or root, root)
            payload = harness_run(root, target_root, no_agent=args.no_agent, force=args.force)
        else:
            print(f"localsetup: unsupported heartbeat action: {args.harness_action}", file=sys.stderr)
            return 2
        print(harness_payload_to_text(payload))
        return 0 if payload.get("ok") else 1

    if args.cmd == "candidate-skill":
        candidate = Path(args.candidate).expanduser()
        if args.candidate_skill_action == "validate":
            payload = validate_candidate_skill(root, candidate, home=home)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0 if payload.get("ok") else 1
        if args.candidate_skill_action == "proposal":
            payload = candidate_skill_proposal(root, candidate, home=home)
            text = candidate_skill_proposal_markdown(payload)
            if args.output == "-":
                print(text, end="")
            else:
                output = Path(args.output).expanduser().resolve()
                output_blockers = candidate_skill_path_blockers(root, output, home=home)
                if output_blockers:
                    print(f"localsetup: candidate-skill proposal output path blocked: {output_blockers[0]}", file=sys.stderr)
                    return 1
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="utf-8")
            return 0 if payload.get("ok") else 1
        print(f"localsetup: unsupported candidate-skill action: {args.candidate_skill_action}", file=sys.stderr)
        return 2

    if args.cmd == "release-docs":
        from .release_docs.cli import main as release_docs_main
        return release_docs_main(["--repo-root", str(root), *args.release_docs_args])

    if args.cmd == "docs-align":
        tool = root / "ls" / "tools" / "docs_alignment.py"
        command = [sys.executable, str(tool), "--repo-root", str(root), *args.docs_align_args]
        return subprocess.run(command, cwd=root).returncode

    if args.cmd == "context-index":
        tool = root / "ls" / "tools" / "context_index.py"
        target_root = Path(getattr(args, "target_directory", None) or root).expanduser().resolve()
        command = [
            sys.executable,
            str(tool),
            "--repo",
            str(target_root),
            "--source-root",
            str(root),
            "--home",
            str(home),
            *args.context_index_args,
        ]
        return subprocess.run(command, cwd=target_root).returncode

    if args.cmd == "catalog":
        skills = []
        for skill in load_skill_catalog(root):
            skills.append(
                {
                    "name": skill.name,
                    "legacy_name": skill.legacy_name,
                    "description": skill.description,
                    "version": skill.version,
                    "class": skill.taxonomy_class,
                    "sort_priority": skill.sort_priority,
                    "tags": skill.tags,
                    "owner_scope": skill.owner_scope,
                    "packs": skill.packs,
                    "path": str(skill.path),
                }
            )
        payload = {
            "skills": skills,
            "workflows": [workflow.__dict__ | {"path": str(workflow.path)} for workflow in load_workflow_catalog(root)],
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.cmd == "diff":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else None
        payload = diff_plan_current(
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
            target_root=target_root,
            attach_mode=config.attach_mode,
        )
        _print_payload(payload)
        return 0

    if args.cmd == "skill":
        payload = skill_payload(root, args.query)
        _print_payload(payload)
        return 0 if payload["count"] else 1

    if args.cmd == "workflow":
        payload = workflow_payload(root, args.query)
        _print_payload(payload)
        return 0 if payload["count"] else 1

    if args.cmd == "why":
        _print_payload(pack_reasoning(root, args.packs))
        return 0

    if args.cmd == "graph":
        _print_payload(graph_payload(root))
        return 0

    if args.cmd == "path":
        if args.json or args.path_action is None:
            payload = build_paths_manifest(root, home)
            payload["manifest"] = str(write_paths_manifest(root, home)["manifest"])
            _print_payload(payload)
            return 0
        if args.path_action in {"source-root", "framework-root", "docs-root", "tools-root", "package-root"}:
            print(resolve_named_path(root, home, args.path_action))
            return 0
        if args.path_action == "package":
            print(resolve_package_path(home, args.name, args.relative_path, package_root=resolve_named_path(root, home, "package-root")))
            return 0
        if args.path_action == "doc":
            print(resolve_doc_path(root, args.relative_path))
            return 0
        if args.path_action == "tool":
            print(resolve_tool_path(root, args.relative_path))
            return 0
        print(f"localsetup: unsupported path action: {args.path_action}", file=sys.stderr)
        return 2

    if args.cmd == "adopt":
        target_root = Path(getattr(args, "target_directory", None) or root).expanduser().resolve()
        _print_payload(adopt_recommendations(target_root))
        return 0

    if args.cmd == "detach":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else root
        if args.skill_scope == "personal":
            from .personal_detach import detach_personal
            payload = detach_personal(root, home, list(config.platforms or []), apply=args.apply)
            _print_payload(payload)
            return 0 if payload["ok"] else 2
        if args.plan:
            raise ValueError("Repository detach planning is not yet supported; no changes made")
        from .detach import detach_platforms
        _print_payload(detach_platforms(root, home, target_root, list(config.platforms or [])))
        return 0

    if args.cmd == "sbom":
        if args.installed:
            target_root = Path(getattr(args, "target_directory", None) or root).expanduser().resolve()
            payload = write_installed_sbom(root, target_root, Path(args.out))
        else:
            payload = write_source_sbom(root, Path(args.out))
        _print_payload(payload)
        return 0

    if args.cmd == "validate-catalog":
        package_surface = validate_package_surfaces(root, home=home)
        issues = (
            validate_manifest_schemas(root)
            + validate_plugin_pack_manifest(root)
            + validate_skill_catalog(root)
            + validate_workflow_catalog(root)
            + [f"package surface: {issue}" for issue in package_surface["issues"]]
        )
        print(json.dumps({"ok": not issues, "issues": issues}, indent=2))
        return 0 if not issues else 1

    if args.cmd == "validate-package-surface":
        payload = validate_package_surfaces(root, home=home)
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "test-workers":
        try:
            payload = test_workers_payload(getattr(args, "workers", None))
        except ValueError as exc:
            print(f"localsetup: invalid test worker configuration: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_payload(payload)
        else:
            print(payload["workers"])
        return 0

    if args.cmd == "scan-migration":
        print(json.dumps({"findings": scan_legacy_references(root, include_expected=args.include_expected)}, indent=2))
        return 0

    if args.cmd == "reprocess-paths":
        if args.apply:
            print("localsetup: reprocess-paths --apply is disabled until allowlisted rewrites are implemented", file=sys.stderr)
            return 2
        payload = reprocess_localsetup_paths(root, apply=bool(args.apply))
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "audit-global-first":
        target_root = Path(getattr(args, "target_directory", None)).expanduser().resolve() if getattr(args, "target_directory", None) else None
        payload = audit_global_first(root, home=home, target_root=target_root)
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "hook-gate":
        result = run_maintainer_gate(root, Path(args.out), runner=args.runner)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    if args.cmd == "version-plan":
        if args.push_stdin:
            plans = push_lines_to_plans(root, sys.stdin.read())
            print_json({"ok": all(plan["ok"] for plan in plans), "plans": plans})
            return 0 if all(plan["ok"] for plan in plans) else 1
        payload = plan_version(root, base=args.base, head=args.head, ref=args.ref)
        print_json(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "version-sync":
        plan = None
        target = args.target
        if target:
            from .versioning_policy import guard_target
            guard_target(root, target)
        if not target:
            plan = plan_version(root, base=args.base, head=args.head)
            if not plan.get("repairable", True) or (not plan["ok"] and plan.get("release_type_required")):
                print_json(plan)
                return 1
            target = plan["target_version"]
        if args.check:
            payload = check_version_files(root, target)
            if plan:
                payload["plan"] = plan
            print_json(payload)
            return 0 if payload["ok"] else 1
        payload = sync_version_files(root, target)
        if args.stage:
            stage_version_files(root)
            payload["staged"] = True
        if args.commit:
            payload["commit"] = commit_version_sync(root, target)
            payload["commit_message"] = f"{VERSION_SYNC_PREFIX} {target}"
        if plan:
            payload["plan"] = plan
        print_json(payload)
        return 0

    if args.cmd == "publish-preflight":
        payload = publish_preflight(root, base=args.base, head=args.head, fix=args.fix)
        print_json(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "release-push":
        plan = plan_version(root)
        if not plan.get("repairable", True) or (not plan["ok"] and plan.get("release_type_required")):
            print_json(plan)
            return 1
        if plan["bump"] != "none" and not plan["ok"]:
            sync_version_files(root, plan["target_version"])
            commit_version_sync(root, plan["target_version"])
            plan = plan_version(root)
            if not plan.get("repairable", True) or (not plan["ok"] and plan.get("release_type_required")):
                print_json(plan)
                return 1
        generated_docs = publish_preflight(root, fix=True)
        if not generated_docs["ok"]:
            print_json(generated_docs)
            return 1
        push_args = args.push_args
        if push_args and push_args[0] == "--":
            push_args = push_args[1:]
        cmd = ["git", "push", *push_args] if push_args else ["git", "push"]
        completed = subprocess.run(cmd, cwd=root)
        return completed.returncode

    if args.cmd == "self-refresh":
        config = _resolved_config(args, home)
        home = Path(config.home or home).expanduser().resolve()
        target_root = Path(config.target_directory).expanduser().resolve() if config.target_directory else root
        git_pre = git_status_snapshot(target_root)
        packs_override = _split_csv(getattr(args, "packs", None)) if getattr(args, "packs", None) is not None else None
        platforms_override = _split_csv(getattr(args, "platforms", None)) if getattr(args, "platforms", None) is not None else None
        payload = _run_self_refresh(
            root,
            config,
            home,
            packs_override=packs_override,
            platforms_override=platforms_override,
            attach_mode_explicit=getattr(args, "mode", None) is not None,
        )
        _record_health_for_payload(
            root=root,
            home=home,
            target_root=target_root,
            operation="self-refresh",
            mode="apply",
            payload=payload,
            git_pre=git_pre,
            git_post=git_status_snapshot(target_root),
            planned_paths=[".localsetup/lock.json", ".localsetup/health.json", ".localsetup/AGENT_STATUS.md"],
        )
        _write_report(config.output.report, payload)
        _print_payload(payload)
        return 0 if payload["ok"] else 1

    if args.cmd == "install-hooks":
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=root, check=True)
        print_json({"ok": True, "core.hooksPath": ".githooks"})
        return 0

    if args.cmd == "register-shell":
        paths = write_paths_manifest(root, home)
        payload = register_shell_command(root, home=home)
        payload["paths_manifest"] = paths["manifest"]
        _print_payload(payload)
        return 0

    if args.cmd == "package":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = build_public_artifact(root, out)
        print(json.dumps(payload, indent=2))
        return 1 if payload.get("leaks") else 0

    if args.cmd == "verify-release":
        payload = verify_release_artifact(
            Path(args.artifact),
            sha256_path=Path(args.sha256) if args.sha256 else None,
            sbom_path=Path(args.sbom) if args.sbom else None,
            expected_commit=args.expected_commit,
            expected_tag=args.expected_tag,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload["ok"] else 1

    return None
