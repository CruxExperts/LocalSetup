"""Private Agent Q registry v2: local key and explicitly pinned peers."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from ls.core.openpgp import SecretReference, normalize_fingerprint

class RegistryError(RuntimeError):
    pass

def _absolute(value: Any) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise RegistryError("absolute path required")
    return Path(value)

def load_registry_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError("registry unavailable or invalid") from exc
    return validate_registry(raw)

def validate_registry(raw: Any, *, require_keys_exist: bool = True) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("version") != 2:
        raise RegistryError("MIGRATION_REQUIRED: registry version 2 required")
    local_id, local, agents = raw.get("local_agent_id"), raw.get("local"), raw.get("agents")
    if not isinstance(local_id, str) or not local_id or not isinstance(local, dict) or not isinstance(agents, dict):
        raise RegistryError("local_agent_id, local and agents required")
    local_fp = normalize_fingerprint(local.get("fingerprint"))
    home = _absolute(local.get("gnupg_home"))
    if require_keys_exist and not home.is_dir():
        raise RegistryError("local GnuPG home unavailable")
    secret = local.get("secret_ref")
    if not isinstance(secret, dict) or set(secret) != {"provider", "name"}:
        raise RegistryError("local.secret_ref requires provider and name")
    SecretReference(secret["provider"], secret["name"])
    local_scope = local.get("expected_scope")
    local_role = local.get("role", "publisher")
    if (not isinstance(local_scope, list) or not local_scope
            or any(not isinstance(x, str) or not x for x in local_scope)
            or local_role not in {"publisher", "owner"}):
        raise RegistryError("local trust scope and role required")
    local_store = _absolute(local.get("trust_state"))
    peers: dict[str, dict[str, Any]] = {}
    pins = {local_fp}
    for agent_id, cfg in agents.items():
        if not isinstance(agent_id, str) or not agent_id or agent_id == local_id or not isinstance(cfg, dict):
            raise RegistryError("invalid peer")
        fp = normalize_fingerprint(cfg.get("fingerprint"))
        if fp in pins:
            raise RegistryError("duplicate pin")
        pins.add(fp)
        scope = cfg.get("expected_scope")
        if not isinstance(scope, list) or not scope or any(not isinstance(x, str) or not x for x in scope):
            raise RegistryError("expected_scope required")
        transports = cfg.get("transports", {})
        if not isinstance(transports, dict):
            raise RegistryError("invalid transports")
        drop = transports.get("file_drop", {})
        if not isinstance(drop, dict):
            raise RegistryError("invalid file_drop")
        roots = {}
        for field in ("allowed_inbound_roots", "allowed_outbound_roots"):
            values = drop.get(field, [])
            if not isinstance(values, list):
                raise RegistryError("invalid roots")
            roots[field] = tuple(_absolute(value) for value in values)
        recipients = cfg.get("expected_recipient_ids", [local_id])
        if (not isinstance(recipients, list) or any(not isinstance(x, str) or not x for x in recipients)
                or local_id not in recipients or len(recipients) != len(set(recipients))):
            raise RegistryError("invalid expected recipients")
        accounts = transports.get("mail_accounts", [])
        if not isinstance(accounts, list) or any(not isinstance(x, str) for x in accounts):
            raise RegistryError("invalid mail accounts")
        addresses = transports.get("mail_addresses", [])
        if (not isinstance(addresses, list) or any(not isinstance(x, str) or "@" not in x for x in addresses)
                or (accounts and not addresses)):
            raise RegistryError("mail addresses required for mail transport")
        role = cfg.get("role", "publisher")
        if role not in {"publisher", "owner"}:
            raise RegistryError("invalid role")
        peers[agent_id] = {"fingerprint": fp, "trust_state": _absolute(cfg.get("trust_state")),
                           "expected_scope": tuple(scope), "role": role, "expected_recipient_ids": tuple(recipients),
                           "mail_accounts": tuple(accounts), "mail_addresses": tuple(addresses), **roots}
    if not peers:
        raise RegistryError("at least one peer required")
    for cfg in peers.values():
        if any(item != local_id and item not in peers for item in cfg["expected_recipient_ids"]):
            raise RegistryError("unknown expected recipient")
    return {"version": 2, "local_agent_id": local_id,
            "local": {"fingerprint": local_fp, "gnupg_home": home, "secret_ref": secret,
                      "trust_state": local_store, "expected_scope": tuple(local_scope), "role": local_role},
            "agents": peers}

def peer(validated: dict[str, Any], agent_id: str) -> dict[str, Any]:
    try:
        return validated["agents"][agent_id]
    except KeyError as exc:
        raise RegistryError("unknown peer") from exc

def file_drop_inbound_roots(validated: dict[str, Any], agent_id: str) -> list[Path]:
    return list(peer(validated, agent_id)["allowed_inbound_roots"])

def require_file_drop_path(validated: dict[str, Any], agent_id: str, path: Path, *, inbound: bool) -> None:
    field = "allowed_inbound_roots" if inbound else "allowed_outbound_roots"
    selected = Path(path).resolve()
    if not any(selected == root.resolve() or root.resolve() in selected.parents for root in peer(validated, agent_id)[field]):
        raise RegistryError("file-drop path denied")

def require_mail_account(validated: dict[str, Any], agent_id: str, account_id: str) -> None:
    if account_id not in peer(validated, agent_id)["mail_accounts"]:
        raise RegistryError("mail account denied")

def require_mail_address(validated: dict[str, Any], agent_id: str, address: str) -> None:
    if not isinstance(address, str) or address.lower() not in {
        item.lower() for item in peer(validated, agent_id)["mail_addresses"]
    }:
        raise RegistryError("mail address denied")
