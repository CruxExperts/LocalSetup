from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from typing import Any

from .interfaces import describe_operation_interface


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RepositoryTarget:
    hostname: str
    owner: str
    repository: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True)
class Operation:
    operation_id: str
    group: str
    kind: str
    resource_id: int | None
    current: Any
    desired: Any
    interface: dict[str, Any]
    required_permissions: tuple[str, ...]
    risk: str
    verification: str
    recovery: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.operation_id,
            "group": self.group,
            "kind": self.kind,
            "resource_id": self.resource_id,
            "current": self.current,
            "desired": self.desired,
            "interface": self.interface,
            "preconditions": {
                "binding": "plan.target",
                "current": self.current,
            },
            "required_permissions": list(self.required_permissions),
            "risk": self.risk,
            "verification": self.verification,
            "recovery": self.recovery,
        }


def make_operation(
    *,
    group: str,
    kind: str,
    resource_id: int | None,
    current: Any,
    desired: Any,
    required_permissions: tuple[str, ...],
    risk: str = "moderate",
    verification: str,
    recovery: str,
) -> Operation:
    interface = describe_operation_interface(kind, current, desired, resource_id)
    identity = {
        "group": group,
        "kind": kind,
        "resource_id": resource_id,
        "current": current,
        "desired": desired,
        "interface": interface,
    }
    operation_id = digest_json(identity)[:24]
    return Operation(
        operation_id=operation_id,
        group=group,
        kind=kind,
        resource_id=resource_id,
        current=current,
        desired=desired,
        interface=interface,
        required_permissions=required_permissions,
        risk=risk,
        verification=verification,
        recovery=recovery,
    )


def plan_digest(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    return digest_json(unsigned)


def canonical_ruleset(value: dict[str, Any]) -> dict[str, Any]:
    conditions = copy.deepcopy(value.get("conditions")) if isinstance(value.get("conditions"), dict) else {}
    ref_name = conditions.get("ref_name")
    if isinstance(ref_name, dict):
        for key in ("include", "exclude"):
            items = ref_name.get(key)
            if isinstance(items, list) and all(isinstance(item, str) for item in items):
                ref_name[key] = sorted(items)
    rules = copy.deepcopy(value.get("rules")) if isinstance(value.get("rules"), list) else []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        parameters = rule.get("parameters")
        if isinstance(parameters, dict) and isinstance(parameters.get("required_status_checks"), list):
            parameters["required_status_checks"].sort(key=lambda row: (str(row.get("context", "")), str(row.get("integration_id", ""))) if isinstance(row, dict) else ("", ""))
            parameters.setdefault("do_not_enforce_on_create", False)
    rules.sort(key=lambda row: str(row.get("type", "")) if isinstance(row, dict) else "")
    bypass = copy.deepcopy(value.get("bypass_actors")) if isinstance(value.get("bypass_actors"), list) else None
    if bypass is not None:
        bypass.sort(key=lambda row: (str(row.get("actor_type", "")), str(row.get("actor_id", "")), str(row.get("bypass_mode", ""))) if isinstance(row, dict) else ("", "", ""))
    return {
        "name": value.get("name"),
        "target": value.get("target"),
        "enforcement": value.get("enforcement"),
        "conditions": conditions,
        "rules": rules,
        "bypass_actors": bypass,
    }
