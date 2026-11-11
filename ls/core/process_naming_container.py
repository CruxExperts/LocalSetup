"""Opt-in Docker/Podman creation boundary for display names, not lifecycle authority."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import subprocess
from typing import Sequence

from .process_identity import NameContext, NameFormat, bounded_name, format_name


@dataclass(frozen=True)
class ContainerNameResult:
    status: str
    container_id: str | None
    full_name: str
    observed_name: str | None = None
    inner_process_status: str = "not_observed"
    returncode: int | None = None


def create_container(
    engine: str,
    image: str,
    context: NameContext,
    *,
    engine_options: Sequence[str] = (),
    create_options: Sequence[str] = (),
    command: Sequence[str] = (),
    options: NameFormat = NameFormat(),
    orchestrated: bool = False,
) -> ContainerNameResult:
    """Create, then inspect by native ID; never start, rename, or adopt containers.

    Callers must mark orchestrator-managed creations and retain their own name
    options in that case. The engine resolves any concurrent name collision.
    This function deliberately does not capture command arguments in metadata.
    """
    if Path(engine).name not in ("docker", "podman"):
        raise ValueError("engine must be docker or podman")
    if not image or image.startswith("-"):
        raise ValueError("an explicit image is required")
    # Do not pass arbitrary engine or create tokens: positional tokens and
    # options consuming following values can move the command/image boundary.
    if engine_options:
        raise ValueError("engine options are not supported")
    if create_options:
        if (not orchestrated or len(create_options) != 2 or create_options[0] != "--name"
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", create_options[1])):
            raise ValueError("only an orchestrator-owned --name VALUE is supported")
    full = format_name(context, options)
    prefix = [engine, *engine_options]
    container_id = None
    try:
        if not orchestrated:
            inventory = subprocess.run([*prefix, "ps", "--all", "--format", "{{.Names}}"],
                                       capture_output=True, text=True, check=False)
            if inventory.returncode:
                return ContainerNameResult("inventory_failed", None, full, returncode=inventory.returncode)
            occupied = set(inventory.stdout.splitlines())
            index = 0
            while True:
                selected = replace(context, index=index)
                native = bounded_name(selected, 128, options)
                if native not in occupied:
                    break
                index += 1
            full = format_name(selected, options)
            naming = ["--name", native, "--label", f"io.localsetup.workload-name={full}"]
        else:
            naming = []
        created = subprocess.run([*prefix, "create", *naming, *create_options, image, *command],
                                 capture_output=True, text=True, check=False)
        if created.returncode:
            return ContainerNameResult("creation_failed", None, full, returncode=created.returncode)
        container_id = created.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            return ContainerNameResult("creation_id_unavailable", None, full)
        inspected = subprocess.run([*prefix, "inspect", "--format", "{{.Name}}", container_id],
                                   capture_output=True, text=True, check=False)
        if inspected.returncode:
            return ContainerNameResult("verification_unavailable", container_id, full, returncode=inspected.returncode)
        observed = inspected.stdout.strip().removeprefix("/")
        if not observed:
            return ContainerNameResult("verification_unavailable", container_id, full)
        if orchestrated:
            expected = create_options[1] if create_options else None
            status = "orchestrator_name_preserved" if expected is None or observed == expected else "verification_failed"
            return ContainerNameResult(status, container_id, full, observed)
        status = "verification_failed" if observed != native else ("truncated" if native != full else "verified")
        return ContainerNameResult(status, container_id, full, observed)
    except (OSError, UnicodeError):
        return ContainerNameResult("verification_unavailable" if container_id else "unavailable", container_id, full)
