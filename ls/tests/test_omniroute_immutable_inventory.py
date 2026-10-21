from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ls.core.aliases import collect_skill_aliases


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ls/skills/ls-omniroute-update/scripts/omniroute_inventory.py"
FIXTURE = ROOT / "ls/tests/fixtures/omniroute/immutable-inventory-7ee5bbc.json"
ENDPOINT_REFERENCE = (
    ROOT / "ls/skills/ls-omniroute-model-discovery/references/omniroute-endpoints.md"
)
ADMIN_ENDPOINT_MATRIX = (
    ROOT
    / "ls/skills/ls-omniroute-admin-automation/references/omniroute-endpoint-matrix.md"
)
UPDATE_WORKFLOW = ROOT / "ls/skills/ls-omniroute-update/references/update-workflow.md"
UPDATE_SKILL = ROOT / "ls/skills/ls-omniroute-update/SKILL.md"
DECISION_SHAPED_PROXY_TOOLS = {
    "omniroute_best_combo_for_task",
    "omniroute_explain_route",
    "omniroute_simulate_route",
}


def _load_inventory():
    spec = importlib.util.spec_from_file_location(
        "omniroute_inventory_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _explicit_mirror() -> Path | None:
    value = os.environ.get("LOCALSETUP_OMNIROUTE_MIRROR")
    return Path(value) if value else None


def _create_retained_layout(root: Path, packages: tuple[str, ...]) -> None:
    for package in packages:
        (root / "ls" / "skills" / package).mkdir(parents=True)


def _project_claim_source_path(source_path: str, aliases: dict[str, str]) -> str:
    parts = source_path.split("/")
    if len(parts) >= 3 and parts[:2] == ["ls", "skills"]:
        parts[2] = aliases.get(parts[2], parts[2])
    return "/".join(parts)


def _project_fixture_retained_claims(
    module, expected: dict, aliases: dict[str, str] | None = None
) -> tuple[dict, dict, dict[str, str]]:
    original = expected["retained_claims"]
    assert module._sha256_json(original) == expected["digests"]["retained_claims"]
    if aliases is None:
        aliases = collect_skill_aliases(ROOT / "ls" / "skills")
    projected = deepcopy(original)
    for collection in ("resolved", "unresolved"):
        rows = projected[collection]
        for row in rows:
            row["package"] = aliases.get(row["package"], row["package"])
            row["source_path"] = _project_claim_source_path(row["source_path"], aliases)
        rows.sort(key=lambda row: (
            row["package"], row.get("kind", ""), row.get("claim", ""), row["source_path"]
        ))
    digests = dict(expected["digests"])
    digests["retained_claims"] = module._sha256_json(projected)
    return projected, digests, aliases


def test_immutable_inventory_help_does_not_require_pyyaml() -> None:
    result = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert result.stderr == ""


def test_immutable_inventory_yaml_load_is_actionable_without_site_packages() -> None:
    runner = "\n".join(
        (
            "import importlib.util",
            "import pathlib",
            "import sys",
            f"script = pathlib.Path({str(SCRIPT)!r})",
            'spec = importlib.util.spec_from_file_location("inventory_without_pyyaml", script)',
            "assert spec is not None and spec.loader is not None",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules[spec.name] = module",
            "spec.loader.exec_module(module)",
            "module._receipt = lambda _git_dir, _path: {}",
            "module._blob = lambda _git_dir, _path: b'paths: {}'",
            "try:",
            "    module._openapi_inventory(pathlib.Path('mirror.git'))",
            "except module.InventoryError as exc:",
            "    print(str(exc), file=sys.stderr)",
            "    raise SystemExit(2)",
            "raise AssertionError('expected missing PyYAML failure')",
        )
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", runner],
        check=False,
        capture_output=True,
        cwd=ROOT,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == (
        "Missing dependency: PyYAML. Run `uv sync --locked --no-dev` from the "
        "LocalSetup source checkout.\n"
    )


def test_immutable_inventory_matches_full_exact_mirror_contract() -> None:
    mirror = _explicit_mirror()
    if mirror is None:
        pytest.skip("requires explicitly supplied OmniRoute mirror")
    module = _load_inventory()
    inventory = module.build_inventory(mirror, ROOT)
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    retained_claims, expected_digests, aliases = _project_fixture_retained_claims(
        module, expected
    )
    proxy_package = aliases.get("ls-omniroute-model-discovery", "ls-omniroute-model-discovery")
    proxy_reference = _project_claim_source_path(
        ENDPOINT_REFERENCE.relative_to(ROOT).as_posix(), aliases
    )

    assert inventory["schema_version"] == expected["schema_version"]
    assert inventory["source"] == expected["source"]
    assert inventory["digests"] == expected_digests
    assert {
        "skills": len(inventory["skills"]),
        "route_handlers": len(inventory["route_handlers"]),
        "openapi_operations": len(inventory["openapi_operations"]),
        "compatibility_rewrites": len(inventory["compatibility_rewrites"]),
        "registered_tools": len(inventory["registered_tools"]),
        "retained_claims_resolved": len(inventory["retained_claims"]["resolved"]),
        "retained_claims_unresolved": len(inventory["retained_claims"]["unresolved"]),
    } == expected["counts"]
    assert [row["name"] for row in inventory["registered_tools"]] == expected[
        "registered_tool_names"
    ]
    assert inventory["retained_claims"] == retained_claims

    for key in (
        "skills",
        "route_handlers",
        "openapi_operations",
        "compatibility_rewrites",
        "registered_tools",
        "retained_claims",
    ):
        assert module._sha256_json(inventory[key]) == expected_digests[key]
    assert module.CLAIM_EXCEPTIONS == {}

    registered = {row["name"] for row in inventory["registered_tools"]}
    assert DECISION_SHAPED_PROXY_TOOLS <= registered
    for false_positive in (
        "omniroute_admin",
        "omniroute_api",
        "omniroute_discover",
        "omniroute_inventory",
        "omniroute_login_time",
        "omniroute_music",
        "omniroute_proxy",
        "omniroute_update",
    ):
        assert false_positive not in registered
    assert all(row["owners"] for row in inventory["registered_tools"])
    assert all(
        owner["source_path"] in module.TOOL_OWNER_PATHS
        and re.fullmatch(r"[0-9a-f]{40}", owner["source_blob"])
        and re.fullmatch(r"[0-9a-f]{64}", owner["source_sha256"])
        for row in inventory["registered_tools"]
        for owner in row["owners"]
    )
    retained_proxy_claims = {
        row["claim"]
        for row in inventory["retained_claims"]["resolved"]
        if row["package"] == proxy_package
    }
    assert not DECISION_SHAPED_PROXY_TOOLS & retained_proxy_claims

    for collection in (inventory["route_handlers"], inventory["openapi_operations"]):
        assert all(row["method"] in module.HTTP_METHODS for row in collection)
        assert all(row["path"].startswith("/") for row in collection)
        assert all(re.fullmatch(r"[0-9a-f]{40}", row["source_blob"]) for row in collection)
        assert all(re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"]) for row in collection)

    rewrites = {(row["source"], row["destination"]) for row in inventory["compatibility_rewrites"]}
    assert ("/v1/:path*", "/api/v1/:path*") in rewrites
    models_claims = [
        row
        for row in inventory["retained_claims"]["resolved"]
        if row["claim"] == "GET /v1/models"
    ]
    assert models_claims
    assert all(row["status"] == "compatible-rewrite" for row in models_claims)
    assert all(row["target"] == "GET /api/v1/models" for row in models_claims)
    wildcard_claims = {
        row["claim"]: row
        for row in inventory["retained_claims"]["resolved"]
        if "targets" in row
    }
    assert {
        claim: wildcard_claims[claim]
        for claim in (
            "GET /api/pricing*",
            "GET /api/usage/*",
            "POST /v1/audio/*",
        )
    } == {
        "GET /api/pricing*": {
            "package": proxy_package,
            "kind": "endpoint",
            "claim": "GET /api/pricing*",
            "source_path": proxy_reference,
            "status": "registered-wildcard",
            "targets": [
                "GET /api/pricing",
                "GET /api/pricing/defaults",
                "GET /api/pricing/models",
                "GET /api/pricing/sync",
            ],
        },
        "GET /api/usage/*": {
            "package": proxy_package,
            "kind": "endpoint",
            "claim": "GET /api/usage/*",
            "source_path": proxy_reference,
            "status": "registered-wildcard",
            "targets": [
                "GET /api/usage/analytics",
                "GET /api/usage/budget",
                "GET /api/usage/budget/bulk",
                "GET /api/usage/call-logs",
                "GET /api/usage/call-logs/{id}",
                "GET /api/usage/combo-forecast",
                "GET /api/usage/combo-health",
                "GET /api/usage/combo-health-autopilot",
                "GET /api/usage/combo-health-dashboard",
                "GET /api/usage/combo-scoring-inspector",
                "GET /api/usage/history",
                "GET /api/usage/logs",
                "GET /api/usage/om-usage",
                "GET /api/usage/provider-limits",
                "GET /api/usage/provider-window-costs",
                "GET /api/usage/proxy-logs",
                "GET /api/usage/quota",
                "GET /api/usage/request-logs",
                "GET /api/usage/requests-by-provider-date",
                "GET /api/usage/route-explain/{id}",
                "GET /api/usage/token-limits",
                "GET /api/usage/utilization",
                "GET /api/usage/{connectionId}",
            ],
        },
        "POST /v1/audio/*": {
            "package": proxy_package,
            "kind": "endpoint",
            "claim": "POST /v1/audio/*",
            "source_path": proxy_reference,
            "status": "compatible-rewrite-wildcard",
            "targets": [
                "POST /api/v1/audio/speech",
                "POST /api/v1/audio/transcriptions",
                "POST /api/v1/audio/translations",
            ],
        },
    }
    assert set(wildcard_claims) == {
        "GET /api/combos*",
        "POST /api/combos*",
        "GET /api/pricing*",
        "GET /api/provider-nodes*",
        "POST /api/provider-nodes*",
        "GET /api/providers*",
        "POST /api/providers*",
        "GET /api/usage/*",
        "POST /v1/audio/*",
    }
    for claim in (
        "GET /api/combos*",
        "POST /api/combos*",
        "GET /api/provider-nodes*",
        "POST /api/provider-nodes*",
        "GET /api/providers*",
        "POST /api/providers*",
    ):
        assert wildcard_claims[claim]["status"] == "registered-wildcard"
        assert wildcard_claims[claim]["targets"]
        assert wildcard_claims[claim]["targets"] == sorted(
            wildcard_claims[claim]["targets"]
        )
    alias_claims = {
        row["claim"]
        for row in inventory["retained_claims"]["resolved"]
        if row["source_path"] == ENDPOINT_REFERENCE.relative_to(ROOT).as_posix()
        and row["claim"].endswith(" /api/models/alias")
    }
    assert alias_claims == {
        "DELETE /api/models/alias",
        "GET /api/models/alias",
        "PUT /api/models/alias",
    }
    assert inventory["retained_claims"]["unresolved"] == []
    assert str(mirror) not in json.dumps(inventory, sort_keys=True)


def test_registered_tool_patterns_reject_plain_text_false_positives() -> None:
    module = _load_inventory()
    text = (
        'const prose = "omniroute_not_registered";\n'
        'server.registerTool("omniroute_registered_direct", {}, handler);\n'
        'const tool = { name: "omniroute_registered_collection" };\n'
    )

    assert module.DIRECT_TOOL_RE.findall(text) == ["omniroute_registered_direct"]
    assert module.NAMED_TOOL_RE.findall(text) == ["omniroute_registered_collection"]
    assert "omniroute_not_registered" not in (
        module.DIRECT_TOOL_RE.findall(text) + module.NAMED_TOOL_RE.findall(text)
    )


def test_retained_claims_parse_endpoint_first_and_method_first_tables_and_prose(
    tmp_path: Path,
) -> None:
    module = _load_inventory()
    _create_retained_layout(tmp_path, module.RETAINED_PACKAGES)
    reference = (
        tmp_path
        / "ls"
        / "skills"
        / "ls-omniroute-model-discovery"
        / "references"
        / "endpoint-table.md"
    )
    reference.parent.mkdir(exist_ok=True)
    reference.write_text(
        "| Endpoint | Method |\n"
        "| --- | --- |\n"
        "| `/api/models/catalog` | GET |\n"
        "| `/v1/models` | GET/POST |\n"
        "| `/api/http-get` | HTTP GET |\n"
        "| `/api/http-post` | HTTP POST |\n"
        "| `/api/http-multi` | HTTP GET/POST |\n"
        "| `/api/unbounded` | GET/POST/etc. |\n"
        "| `/api/arbitrary` | runtime GET |\n"
        "| `/api/unknown` | HTTP INVOKE |\n"
        "\n| Method | Endpoint |\n"
        "| --- | --- |\n"
        "| GET | `/api/method-first` |\n"
        "| GET/POST | `/api/method-multi` |\n"
        "| HTTP GET | `/api/http-method-first` |\n"
        "| HTTP GET/POST | `/api/http-method-multi` |\n"
        "| INVOKE | `/api/unknown-method` |\n"
        "| runtime GET | `/api/arbitrary-method` |\n"
        "| GET/POST/etc. | `/api/unbounded-method` |\n"
        "\nGET /v1/models\n",
        encoding="utf-8",
    )

    claims = module._retained_claims(tmp_path)

    assert {
        row["claim"]
        for row in claims
        if row["package"] == "ls-omniroute-model-discovery" and row["kind"] == "endpoint"
    } == {
        "GET /api/http-get",
        "GET /api/http-method-first",
        "GET /api/http-method-multi",
        "GET /api/http-multi",
        "GET /api/method-first",
        "GET /api/method-multi",
        "GET /api/models/catalog",
        "GET /v1/models",
        "POST /api/http-method-multi",
        "POST /api/http-multi",
        "POST /api/method-multi",
        "POST /api/http-post",
        "POST /v1/models",
    }


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("ls", "inventory_retained_ls_symlink"),
        ("skills", "inventory_retained_skills_symlink"),
        ("package", "inventory_retained_package_symlink"),
    ],
)
def test_retained_claims_rejects_symlinked_roots(
    tmp_path: Path,
    kind: str,
    error: str,
) -> None:
    module = _load_inventory()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    if kind == "ls":
        (root / "ls").symlink_to(outside, target_is_directory=True)
    elif kind == "skills":
        (root / "ls").mkdir()
        (root / "ls" / "skills").symlink_to(outside, target_is_directory=True)
    else:
        (root / "ls" / "skills").mkdir(parents=True)
        (root / "ls" / "skills" / module.RETAINED_PACKAGES[0]).symlink_to(
            outside, target_is_directory=True
        )

    with pytest.raises(module.InventoryError, match=f"^{error}$"):
        module._retained_claims(root)


def test_retained_claims_accepts_real_in_root_package_layout(tmp_path: Path) -> None:
    module = _load_inventory()
    _create_retained_layout(tmp_path, module.RETAINED_PACKAGES)
    reference = (
        tmp_path
        / "ls"
        / "skills"
        / module.RETAINED_PACKAGES[0]
        / "references"
        / "claims.md"
    )
    reference.parent.mkdir()
    reference.write_text("GET /v1/models\n", encoding="utf-8")

    assert module._retained_claims(tmp_path) == [
        {
            "package": module.RETAINED_PACKAGES[0],
            "kind": "endpoint",
            "claim": "GET /v1/models",
            "source_path": reference.relative_to(tmp_path).as_posix(),
        }
    ]


def test_immutable_inventory_documented_invocation_requires_retained_claim_root() -> None:
    command = (
        "python3 ls/skills/ls-omniroute-update/scripts/omniroute_inventory.py "
        "--git-dir <bare-mirror> --localsetup-root <repo-root>"
    )

    assert command in UPDATE_WORKFLOW.read_text(encoding="utf-8")
    skill_text = UPDATE_SKILL.read_text(encoding="utf-8")
    assert "Retained LocalSetup claim references are a separate local input" in skill_text
    assert "neither the mirror root nor local claim root path is emitted" in skill_text


def test_actual_endpoint_reference_emits_every_bounded_endpoint_first_table_claim() -> None:
    module = _load_inventory()
    method_cell = re.compile(
        r"(?:HTTP[ \t]+)?"
        r"((?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)"
        r"(?:/(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT))*)"
    )
    expected: set[str] = set()
    for line in ENDPOINT_REFERENCE.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        endpoint = cells[0].strip("`")
        match = method_cell.fullmatch(cells[1])
        if endpoint.startswith("/") and match:
            expected.update(
                f"{method} {endpoint}" for method in match.group(1).split("/")
            )

    actual = {
        row["claim"]
        for row in module._retained_claims(ROOT)
        if row["package"] == "ls-omniroute-model-discovery"
        and row["kind"] == "endpoint"
        and row["source_path"] == ENDPOINT_REFERENCE.relative_to(ROOT).as_posix()
    }

    assert expected <= actual
    assert "POST /api/models/alias" not in actual


def test_actual_admin_endpoint_matrix_emits_every_bounded_method_first_table_claim() -> None:
    module = _load_inventory()
    method_cell = re.compile(
        r"(?:HTTP[ \t]+)?"
        r"((?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)"
        r"(?:/(?:DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT))*)"
    )
    expected: set[str] = set()
    for line in ADMIN_ENDPOINT_MATRIX.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) < 2:
            continue
        match = method_cell.fullmatch(cells[0])
        endpoint = cells[1].strip("`")
        if match and endpoint.startswith("/"):
            expected.update(
                f"{method} {endpoint}" for method in match.group(1).split("/")
            )

    actual = {
        row["claim"]
        for row in module._retained_claims(ROOT)
        if row["package"] == "ls-omniroute-admin-automation"
        and row["kind"] == "endpoint"
        and row["source_path"]
        == ADMIN_ENDPOINT_MATRIX.relative_to(ROOT).as_posix()
    }

    assert expected <= actual


def test_configuration_suffix_wildcards_are_allowlisted_route_only_and_deterministic() -> None:
    module = _load_inventory()
    claims = [
        {
            "package": "test-package",
            "kind": "endpoint",
            "claim": claim,
            "source_path": "ls/skills/test-package/reference.md",
        }
        for claim in (
            "GET /api/providers*",
            "POST /api/providers*",
            "GET /api/provider-nodes*",
            "POST /api/provider-nodes*",
            "GET /api/combos*",
            "POST /api/combos*",
            "GET /api/future*",
        )
    ]
    routes = [
        {"method": "GET", "path": "/api/providers"},
        {"method": "GET", "path": "/api/providers/client"},
        {"method": "POST", "path": "/api/providers"},
        {"method": "POST", "path": "/api/providers/validate"},
        {"method": "DELETE", "path": "/api/providers/{id}"},
        {"method": "GET", "path": "/api/providers-private"},
        {"method": "GET", "path": "/api/provider-nodes"},
        {"method": "POST", "path": "/api/provider-nodes"},
        {"method": "POST", "path": "/api/provider-nodes/validate"},
        {"method": "GET", "path": "/api/combos"},
        {"method": "GET", "path": "/api/combos/metrics"},
        {"method": "POST", "path": "/api/combos"},
        {"method": "POST", "path": "/api/combos/reorder"},
        {"method": "DELETE", "path": "/api/combos/metrics"},
    ]
    openapi = [
        {"method": "GET", "path": "/api/providers/openapi-only"},
        {"method": "POST", "path": "/api/combos/openapi-only"},
    ]

    resolved = module._resolve_claims(
        Path("portable.git"), claims, routes, openapi, [], []
    )

    assert resolved["resolved"] == [
        {
            **claims[0],
            "status": "registered-wildcard",
            "targets": ["GET /api/providers", "GET /api/providers/client"],
        },
        {
            **claims[1],
            "status": "registered-wildcard",
            "targets": ["POST /api/providers", "POST /api/providers/validate"],
        },
        {
            **claims[2],
            "status": "registered-wildcard",
            "targets": ["GET /api/provider-nodes"],
        },
        {
            **claims[3],
            "status": "registered-wildcard",
            "targets": [
                "POST /api/provider-nodes",
                "POST /api/provider-nodes/validate",
            ],
        },
        {
            **claims[4],
            "status": "registered-wildcard",
            "targets": ["GET /api/combos", "GET /api/combos/metrics"],
        },
        {
            **claims[5],
            "status": "registered-wildcard",
            "targets": ["POST /api/combos", "POST /api/combos/reorder"],
        },
    ]
    assert resolved["unresolved"] == [claims[6]]


def test_documented_suffix_wildcards_require_exact_source_routes_and_rewrite_first() -> None:
    module = _load_inventory()
    claims = [
        {
            "package": "test-package",
            "kind": "endpoint",
            "claim": "GET /api/pricing*",
            "source_path": "ls/skills/test-package/reference.md",
        },
        {
            "package": "test-package",
            "kind": "endpoint",
            "claim": "GET /api/usage/*",
            "source_path": "ls/skills/test-package/reference.md",
        },
        {
            "package": "test-package",
            "kind": "endpoint",
            "claim": "POST /v1/audio/*",
            "source_path": "ls/skills/test-package/reference.md",
        },
        {
            "package": "test-package",
            "kind": "endpoint",
            "claim": "POST /api/pricing*",
            "source_path": "ls/skills/test-package/reference.md",
        },
    ]
    routes = [
        {"method": "GET", "path": "/api/pricing"},
        {"method": "GET", "path": "/api/pricing/defaults"},
        {"method": "GET", "path": "/api/pricing-private"},
        {"method": "POST", "path": "/api/pricing/forbidden-method"},
        {"method": "GET", "path": "/api/usage"},
        {"method": "GET", "path": "/api/usage/analytics"},
        {"method": "GET", "path": "/api/usage-private"},
        {"method": "GET", "path": "/api/v1/audio/speech"},
        {"method": "POST", "path": "/api/v1/audio/speech"},
        {"method": "POST", "path": "/api/v1/audio/translations"},
    ]
    openapi = [{"method": "GET", "path": "/api/usage/openapi-only"}]
    rewrites = [{"source": "/v1/:path*", "destination": "/api/v1/:path*"}]

    resolved = module._resolve_claims(
        Path("portable.git"), claims, routes, openapi, rewrites, []
    )

    assert resolved["resolved"] == [
        {
            **claims[0],
            "status": "registered-wildcard",
            "targets": ["GET /api/pricing", "GET /api/pricing/defaults"],
        },
        {
            **claims[1],
            "status": "registered-wildcard",
            "targets": ["GET /api/usage/analytics"],
        },
        {
            **claims[2],
            "status": "compatible-rewrite-wildcard",
            "targets": [
                "POST /api/v1/audio/speech",
                "POST /api/v1/audio/translations",
            ],
        },
    ]
    assert resolved["unresolved"] == [claims[3]]


def test_inventory_requires_explicit_localsetup_root_for_api_and_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_inventory()
    monkeypatch.setattr(module, "_source_provenance", lambda _git_dir: {})

    with pytest.raises(
        module.InventoryError, match="^inventory_localsetup_root_required$"
    ):
        module.build_inventory(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        module.main(["--git-dir", str(tmp_path)])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the following arguments are required: --localsetup-root" in captured.err


def test_immutable_inventory_cli_accepts_explicit_localsetup_root(
    capsys: pytest.CaptureFixture[str],
) -> None:
    mirror = _explicit_mirror()
    if mirror is None:
        pytest.skip("requires explicitly supplied OmniRoute mirror")
    module = _load_inventory()
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    retained_claims, expected_digests, _aliases = _project_fixture_retained_claims(
        module, expected
    )

    assert module.main(
        [
            "--git-dir",
            str(mirror),
            "--localsetup-root",
            str(ROOT),
        ]
    ) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    inventory = json.loads(captured.out)
    assert inventory["source"] == expected["source"]
    assert inventory["digests"] == expected_digests
    assert inventory["retained_claims"] == retained_claims
    assert inventory["retained_claims"]["unresolved"] == []
    assert str(mirror) not in captured.out
    assert str(ROOT) not in captured.out


def test_immutable_inventory_snapshot_is_well_formed() -> None:
    fixture_bytes = FIXTURE.read_bytes()
    expected = json.loads(fixture_bytes)
    module = _load_inventory()
    authored_aliases = collect_skill_aliases(ROOT / "ls" / "skills")
    projection_aliases = dict(authored_aliases)
    proxy_package = next(
        row["package"]
        for row in expected["retained_claims"]["resolved"]
        if row["source_path"].endswith("/omniroute-endpoints.md")
    )
    projection_aliases.setdefault(proxy_package, "ls-omniroute-model-discovery")
    projected_claims, projected_digests, _ = _project_fixture_retained_claims(
        module, expected, projection_aliases
    )
    assert FIXTURE.read_bytes() == fixture_bytes
    assert projected_digests == {
        **expected["digests"],
        "retained_claims": module._sha256_json(projected_claims),
    }
    for collection in ("resolved", "unresolved"):
        original_rows = expected["retained_claims"][collection]
        projected_rows = projected_claims[collection]
        assert sorted(
            json.dumps(
                {key: value for key, value in row.items() if key not in {"package", "source_path"}},
                sort_keys=True,
            )
            for row in projected_rows
        ) == sorted(
            json.dumps(
                {key: value for key, value in row.items() if key not in {"package", "source_path"}},
                sort_keys=True,
            )
            for row in original_rows
        )
        assert sorted((row["package"], row["source_path"]) for row in projected_rows) == sorted(
            (
                projection_aliases.get(row["package"], row["package"]),
                _project_claim_source_path(row["source_path"], projection_aliases),
            )
            for row in original_rows
        )

    assert expected["schema_version"] == 3
    assert expected["source"]["tag"] == "v3.8.48"
    assert expected["source"]["tag_object"] == (
        "4f00f84b5a12f90fca2f1d72a60404cf6f5bf059"
    )
    assert expected["source"]["commit"] == (
        "7ee5bbc64dbb03e967521227f2afffeb7c9dad1e"
    )
    assert expected["counts"]["skills"] == 44
    assert expected["counts"]["retained_claims_resolved"] == 218
    assert expected["counts"]["retained_claims_unresolved"] == 0
    assert len(expected["registered_tool_names"]) == expected["counts"]["registered_tools"]
    assert expected["registered_tool_names"] == sorted(
        set(expected["registered_tool_names"])
    )
    assert DECISION_SHAPED_PROXY_TOOLS <= set(expected["registered_tool_names"])
    retained_claims = expected["retained_claims"]
    assert set(retained_claims) == {"resolved", "unresolved"}
    assert len(retained_claims["resolved"]) == expected["counts"]["retained_claims_resolved"]
    assert len(retained_claims["unresolved"]) == expected["counts"]["retained_claims_unresolved"]
    assert retained_claims["resolved"] == sorted(
        retained_claims["resolved"],
        key=lambda row: (
            row["package"],
            row["kind"],
            row["claim"],
            row["source_path"],
        ),
    )
    for row in retained_claims["resolved"]:
        common = {"package", "kind", "claim", "source_path", "status"}
        assert authored_aliases.get(row["package"], row["package"]) in module.RETAINED_PACKAGES
        assert row["kind"] in {"endpoint", "tool"}
        assert row["source_path"].startswith("ls/skills/")
        assert not Path(row["source_path"]).is_absolute()
        if row["status"] in {"registered-wildcard", "compatible-rewrite-wildcard"}:
            assert set(row) == common | {"targets"}
            assert row["kind"] == "endpoint"
            assert row["targets"] == sorted(set(row["targets"]))
            assert all(
                target.split(" ", 1)[0] in module.HTTP_METHODS
                and target.split(" ", 1)[1].startswith("/")
                for target in row["targets"]
            )
        else:
            assert set(row) == common | {"target"}
    assert module._sha256_json(retained_claims) == expected["digests"]["retained_claims"]
    assert str(ROOT) not in json.dumps(retained_claims, sort_keys=True)
    assert retained_claims["unresolved"] == []
    assert all(
        set(row) == {"package", "kind", "claim", "source_path"}
        and authored_aliases.get(row["package"], row["package"]) in module.RETAINED_PACKAGES
        and row["kind"] == "endpoint"
        and row["source_path"].startswith("ls/skills/")
        for row in retained_claims["unresolved"]
    )
    retained_proxy_claims = {
        row["claim"]
        for row in retained_claims["resolved"]
        if row["source_path"].endswith("/omniroute-endpoints.md")
    }
    assert not DECISION_SHAPED_PROXY_TOOLS & retained_proxy_claims
    for value in expected["digests"].values():
        assert re.fullmatch(r"[0-9a-f]{64}", value)


def test_immutable_inventory_missing_mirror_is_typed_and_sanitized(
    tmp_path: Path,
) -> None:
    module = _load_inventory()

    with pytest.raises(module.InventoryError, match="^inventory_mirror_missing$"):
        module.build_inventory(tmp_path / "missing.git", ROOT)


def test_no_machine_specific_mirror_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALSETUP_OMNIROUTE_MIRROR", raising=False)

    assert _explicit_mirror() is None


def _mock_source_provenance(
    module, monkeypatch: pytest.MonkeyPatch, *, tag_object: str | None = None,
    object_type: str = "tag", peel_error: bool = False,
) -> list[str]:
    calls: list[str] = []
    tag_object = tag_object or module.SOURCE_TAG_OBJECT
    values = {
        f"refs/tags/{module.SOURCE_TAG}": tag_object,
        f"{module.SOURCE_TAG_OBJECT}^{{commit}}": module.SOURCE_COMMIT,
        f"{module.SOURCE_COMMIT}^{{tree}}": module.SOURCE_TREE,
        f"{module.SOURCE_COMMIT}:skills": module.SOURCE_SKILLS_TREE,
    }

    def fake_object_id(_git_dir: Path, expression: str) -> str:
        calls.append(expression)
        if peel_error and expression.endswith("^{commit}"):
            raise module.InventoryError("inventory_git_read_failed")
        try:
            return values[expression]
        except KeyError:
            raise module.InventoryError("inventory_git_read_failed") from None

    def fake_git(_git_dir: Path, *args: str, **_kwargs: object) -> str:
        assert args == ("cat-file", "-t", module.SOURCE_TAG_OBJECT)
        return f"{object_type}\n"

    monkeypatch.setattr(module, "_object_id", fake_object_id)
    monkeypatch.setattr(module, "_git", fake_git)
    return calls


def test_source_tag_provenance_is_complete_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_inventory()
    calls = _mock_source_provenance(module, monkeypatch)

    source = module._source_provenance(Path("portable.git"))

    assert source == {
        "tag": module.SOURCE_TAG,
        "tag_object": module.SOURCE_TAG_OBJECT,
        "commit": module.SOURCE_COMMIT,
        "tree": module.SOURCE_TREE,
        "skills_tree": module.SOURCE_SKILLS_TREE,
    }
    assert calls == [
        f"refs/tags/{module.SOURCE_TAG}",
        f"{module.SOURCE_TAG_OBJECT}^{{commit}}",
        f"{module.SOURCE_COMMIT}^{{tree}}",
        f"{module.SOURCE_COMMIT}:skills",
    ]


def test_source_tag_missing_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_inventory()

    def missing_tag(_git_dir: Path, _expression: str) -> str:
        raise module.InventoryError("inventory_git_read_failed")

    monkeypatch.setattr(module, "_object_id", missing_tag)
    with pytest.raises(module.InventoryError, match="^inventory_source_tag_missing$"):
        module._source_provenance(Path("portable.git"))


def test_source_tag_mismatch_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_inventory()
    _mock_source_provenance(module, monkeypatch, tag_object="0" * 40)

    with pytest.raises(module.InventoryError, match="^inventory_source_tag_mismatch$"):
        module._source_provenance(Path("portable.git"))


def test_source_tag_must_be_annotated(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_inventory()
    _mock_source_provenance(module, monkeypatch, object_type="commit")

    with pytest.raises(
        module.InventoryError, match="^inventory_source_tag_not_annotated$"
    ):
        module._source_provenance(Path("portable.git"))


def test_source_tag_peel_failure_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_inventory()
    _mock_source_provenance(module, monkeypatch, peel_error=True)

    with pytest.raises(module.InventoryError, match="^inventory_source_tag_peel_failed$"):
        module._source_provenance(Path("portable.git"))


def test_build_inventory_cannot_bypass_source_tag_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_inventory()
    calls: list[str] = []

    def source_provenance(_git_dir: Path) -> dict[str, str]:
        calls.append("source")
        return {}

    def tracked_paths(_git_dir: Path) -> list[str]:
        assert calls == ["source"]
        raise RuntimeError("stop-after-provenance")

    monkeypatch.setattr(module, "_source_provenance", source_provenance)
    monkeypatch.setattr(module, "_tracked_paths", tracked_paths)
    with pytest.raises(RuntimeError, match="^stop-after-provenance$"):
        module.build_inventory(tmp_path, ROOT)
