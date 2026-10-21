#!/usr/bin/env python3
"""Thin read-only OmniRoute discovery and model-observation CLI."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


SHARED_LIB = Path(__file__).resolve().parents[3] / "lib"
PACKAGE_LIB = Path(__file__).resolve().parent / "lib"
for path in (SHARED_LIB, PACKAGE_LIB):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from deps import require_deps  # noqa: E402

require_deps(["requests", "jsonschema"])

from omniroute_proxy.cli import build_parser, main as _main  # noqa: E402
from omniroute_proxy.common import (  # noqa: E402
    ACCESS_TARGETS,
    DEFAULT_BASE_URL,
    TARGETS,
    endpoint_hint,
    join_url,
    load_api_key,
    parse_base_url,
    redact_text,
    sanitize_text,
)
from omniroute_proxy.observation import (  # noqa: E402
    build_model_observation,
    run_model_observation,
)
from omniroute_proxy.observation_contract import (  # noqa: E402
    ADAPTER_COMMIT as SOURCE_COMMIT,
    ADAPTER_VERSION as SOURCE_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    ObservationError,
    validate_observation,
)
from omniroute_proxy.probe import (  # noqa: E402
    access_preflight as _access_preflight,
    env_registration_commands,
    fetch_json,
    render_markdown,
    render_preflight_markdown,
    run_probe,
    summarize_payload,
)


def access_preflight(
    base_url: str,
    api_key_env: str,
    api_key: str | None,
    required_access: str,
    timeout: float,
    include_env_commands: bool,
) -> dict[str, Any]:
    """Compatibility wrapper that preserves entrypoint-level fetch monkeypatching."""
    return _access_preflight(
        base_url,
        api_key_env,
        api_key,
        required_access,
        timeout,
        include_env_commands,
        fetcher=fetch_json,
    )


def main() -> int:
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
