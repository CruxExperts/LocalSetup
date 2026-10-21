from __future__ import annotations

from ls.tests.test_install_flow import *

from ls.core.manifests import load_pack_config
from ls.core.lockfile import save_json
from ls.core.paths import expand_user_path

def test_detach_preserves_custom_adapter_entries(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    tool = root / "ls" / "tools" / "localsetup.py"
    apply_plan(
        root,
        build_install_plan(
            root,
            home=home,
            global_packs=["core"],
            repo_preset="custom",
            repo_skills=["localsetup-context"],
            platform_ids=["codex"],
        ),
        home=home,
    )
    custom = root / ".agents" / "skills" / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "detach", "--tools", "codex"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"
    assert not (root / ".agents" / "skills" / "ls-localsetup-project-context").exists()
    assert not (root / ".agents" / "skills" / ".localsetup-adapter.json").exists()
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()


def test_detach_preserves_historical_portable_adapter_content(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    tool = root / "ls" / "tools" / "localsetup.py"
    global_root = home / ".local" / "share" / "localsetup" / "packages"
    managed = global_root / "ls-localsetup-project-context"
    managed.mkdir(parents=True)
    (managed / MARKER_JSON).write_text("{}\n", encoding="utf-8")
    adapter = root / ".codex" / "skills"
    adapter.mkdir(parents=True)
    (adapter / ".localsetup-portable").write_text("managed_by=localsetup\n", encoding="utf-8")
    shutil.copytree(managed, adapter / "ls-localsetup-project-context")
    custom = adapter / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "detach", "--tools", "codex"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert (adapter / "ls-localsetup-project-context").is_dir()
    assert (adapter / ".localsetup-portable").is_file()
    assert (custom / "SKILL.md").read_text(encoding="utf-8") == "# Custom\n"


def test_detach_shared_adapter_preserves_remaining_owner_and_rewrites_lock(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    tool = root / "ls" / "tools" / "localsetup.py"
    apply_plan(
        root,
        build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "cursor"]),
        home=home,
    )
    custom = root / ".agents" / "skills" / "custom-skill"
    custom.mkdir()
    (custom / "SKILL.md").write_text("# Custom\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(tool), "--repo", str(root), "--home", str(home), "detach", "--tools", "codex"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert (root / ".agents" / "skills" / "ls-localsetup-project-context").is_symlink()
    assert (custom / "SKILL.md").is_file()
    lock = load_json(root / ".localsetup" / "lock.json")
    assert lock["platforms"] == ["cursor"]
    shared = next(item for item in lock["adapter_targets"] if item["path"] == str(root / ".agents" / "skills"))
    assert shared["platforms"] == ["cursor"]
    assert shared["owners"] == [{"scope": "repo", "root": str(root.resolve()), "client": "cursor"}]
    assert verify_install(root, home, platform_ids=["cursor"])["ok"] is True


def test_scoped_verify_ignores_other_platform_historical_exposure(tmp_path: Path) -> None:
    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(
        root,
        build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "cursor"]),
        home=home,
    )
    historical = root / ".codex" / "skills"
    historical.parent.mkdir(parents=True, exist_ok=True)
    historical.symlink_to(home / ".local" / "share" / "localsetup" / "packages", target_is_directory=True)

    assert verify_install(root, home, platform_ids=["cursor"])["ok"] is True
    assert verify_install(root, home, platform_ids=["codex"])["ok"] is False
    assert verify_install(root, home)["ok"] is False


@pytest.mark.parametrize("legacy", [False, True])
def test_partial_detach_rewrites_registry_adapter_membership(tmp_path: Path, legacy: bool) -> None:
    from ls.core.detach import detach_platforms
    from ls.core.registry import load_registry

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "cursor"]), home=home)

    if legacy:
        lock_path = root / ".localsetup" / "lock.json"
        old_lock = load_json(lock_path)
        for item in old_lock["adapter_targets"]:
            item.pop("owners", None)
        lock_path.write_text(json.dumps(old_lock), encoding="utf-8")

    detach_platforms(root, home, root, ["codex"])

    pack = load_pack_config(root)
    registry = load_registry(expand_user_path(pack.global_registry, home))
    receipt = registry["targets"][str(root.resolve())]
    shared = next(item for item in receipt["adapters"] if item["path"] == str(root / ".agents" / "skills"))
    assert shared["platforms"] == ["cursor"]
    assert shared["owners"] == [{"scope": "repo", "root": str(root.resolve()), "client": "cursor"}]
    assert all("codex" not in item.get("platforms", []) for item in receipt["adapters"])


def test_last_owner_detach_removes_target_receipt_but_preserves_global_packages(tmp_path: Path) -> None:
    from ls.core.detach import detach_platforms
    from ls.core.registry import load_registry

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)
    pack = load_pack_config(root)
    registry_path = expand_user_path(pack.global_registry, home)
    global_package = expand_user_path(pack.global_root, home) / "ls-localsetup-project-context"

    detach_platforms(root, home, root, ["codex"])

    registry = load_registry(registry_path)
    assert str(root.resolve()) not in registry["targets"]
    assert global_package.is_dir()


def test_last_owner_detach_retains_cross_target_package_reference(tmp_path: Path) -> None:
    from ls.core.detach import detach_platforms
    from ls.core.registry import load_registry

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for target in (first, second):
        apply_plan(
            root,
            build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"], target_root=target),
            home=home,
            target_root=target,
        )

    detach_platforms(root, home, first, ["codex"])

    registry = load_registry(expand_user_path(load_pack_config(root).global_registry, home))
    assert str(first.resolve()) not in registry["targets"]
    assert registry["packages"]["ls-localsetup-project-context"]["refs"] == [str(second.resolve())]
    assert (home / ".local/share/localsetup/packages/ls-localsetup-project-context").is_dir()


@pytest.mark.parametrize("failure_receipt", ["lock", "registry"])
def test_detach_save_failure_restores_filesystem_lock_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_receipt: str
) -> None:
    import ls.core.detach as detach_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(
        root,
        build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "claude-code"]),
        home=home,
    )
    adapter = root / ".agents" / "skills"
    lock_path = root / ".localsetup" / "lock.json"
    registry_path = expand_user_path(load_pack_config(root).global_registry, home)
    lock_before = lock_path.read_bytes()
    registry_before = registry_path.read_bytes()
    original_save = detach_mod.save_json

    def fail_selected(path: Path, payload: dict) -> None:
        if (failure_receipt == "lock" and Path(path) == lock_path) or (
            failure_receipt == "registry" and Path(path) == registry_path
        ):
            raise OSError(f"simulated {failure_receipt} save failure")
        original_save(path, payload)

    monkeypatch.setattr(detach_mod, "save_json", fail_selected)

    with pytest.raises(OSError, match=f"simulated {failure_receipt} save failure"):
        detach_mod.detach_platforms(root, home, root, ["codex"])

    assert (adapter / "ls-localsetup-project-context").is_symlink()
    assert lock_path.read_bytes() == lock_before
    assert registry_path.read_bytes() == registry_before


def test_detach_reloads_writer_state_only_after_package_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager
    import ls.core.detach as detach_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(
        root,
        build_install_plan(root, home=home, packs=["core"], platform_ids=["codex", "cursor"]),
        home=home,
    )
    lock_path = root / ".localsetup" / "lock.json"
    registry_path = expand_user_path(load_pack_config(root).global_registry, home)

    @contextmanager
    def conforming_writer_finishes_before_acquisition(*args: object, **kwargs: object):
        lock = load_json(lock_path)
        lock["conforming_writer_marker"] = "lock-current"
        save_json(lock_path, lock)
        registry = load_json(registry_path)
        registry["conforming_writer_marker"] = "registry-current"
        save_json(registry_path, registry)
        yield {"path": "simulated"}

    monkeypatch.setattr(detach_mod, "package_root_lock", conforming_writer_finishes_before_acquisition)

    detach_mod.detach_platforms(root, home, root, ["codex"])

    lock = load_json(lock_path)
    registry = load_json(registry_path)
    assert lock["conforming_writer_marker"] == "lock-current"
    assert registry["conforming_writer_marker"] == "registry-current"
    assert lock["platforms"] == ["cursor"]
    receipt = registry["targets"][str(root.resolve())]
    assert all("codex" not in item.get("platforms", []) for item in receipt["adapters"])


def test_last_owner_registry_unlink_failure_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ls.core.detach as detach_mod

    root = make_temp_repo(tmp_path)
    home = tmp_path / "home"
    apply_plan(root, build_install_plan(root, home=home, packs=["core"], platform_ids=["codex"]), home=home)
    adapter = root / ".agents" / "skills"
    lock_path = root / ".localsetup" / "lock.json"
    registry_path = expand_user_path(load_pack_config(root).global_registry, home)
    lock_before = lock_path.read_bytes()
    registry_before = registry_path.read_bytes()
    monkeypatch.setattr(
        detach_mod,
        "_unlink_registry",
        lambda path: (_ for _ in ()).throw(OSError("permanent registry unlink failure")),
    )

    with pytest.raises(OSError, match="permanent registry unlink failure"):
        detach_mod.detach_platforms(root, home, root, ["codex"])

    assert (adapter / "ls-localsetup-project-context").is_symlink()
    assert lock_path.read_bytes() == lock_before
    assert registry_path.read_bytes() == registry_before
