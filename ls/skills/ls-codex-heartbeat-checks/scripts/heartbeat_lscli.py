"""Typed heartbeat coding profile; load framework owners only when selected."""
from importlib import import_module
from pathlib import Path
import sys

PATHS = ("executable", "profiles", "grant", "runtime_root", "state_root", "resource_parent")
LIMITS = {"timeout_seconds": (1, 3600), "request_limit": (1, 64),
          "tool_limit": (0, 256), "token_limit": (1, 1048576),
          "no_progress_seconds": (1, 3600), "output_limit_bytes": (1024, 4194304)}


def _resolve(executable, root):
    framework = Path(__file__).resolve().parents[3]
    expected = framework / "core/agent/registration_dispatch.py"
    if not expected.is_file():
        raise ValueError("Typed LSCli profiles require the owning framework installation")
    for name, module in tuple(sys.modules.items()):
        if name == "ls" or name.startswith("ls."):
            source = getattr(module, "__file__", None)
            locations = [source] if source is not None else list(getattr(module, "__path__", ()))
            if not locations or any(not Path(item).resolve().is_relative_to(framework) for item in locations):
                raise ValueError("Ambient framework imports cannot resolve a heartbeat launcher")
    sys.path.insert(0, str(framework.parent))
    try:
        owner = import_module("ls.core.agent.registration_dispatch")
        if Path(owner.__file__).resolve() != expected:
            raise ValueError("Heartbeat registration owner origin mismatch")
        return owner.resolve(executable, root)
    finally:
        sys.path.remove(str(framework.parent))


def plan(profile: dict, agent: dict, name: str, target_root: Path) -> dict:
    required = set(PATHS) | set(LIMITS) | {"client", "launcher", "profile", "prompt"}
    if set(profile) != required or profile["client"] != "lscli" or profile["launcher"] != "lscli":
        raise ValueError("Typed LSCli profile requires exactly its documented fields")
    if set(agent) - {"enabled", "profile", "timeout_seconds"} or agent.get("enabled") is not True:
        raise ValueError("Typed LSCli agent requires explicit enabled/profile and optional timeout")
    paths = {}
    for key in PATHS:
        value = profile[key]
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("Typed LSCli paths require absolute canonical strings")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError("Typed LSCli paths require absolute canonical strings")
        paths[key] = path
    if not isinstance(target_root, Path) or not target_root.is_absolute():
        raise ValueError("Typed LSCli requires the explicit heartbeat target")
    limits = {key: profile[key] for key in LIMITS}
    if "timeout_seconds" in agent:
        limits["timeout_seconds"] = agent["timeout_seconds"]
    for key, (low, high) in LIMITS.items():
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError("Typed LSCli limits require documented bounded integers")
    if limits["no_progress_seconds"] > limits["timeout_seconds"]:
        raise ValueError("Protocol activity timeout exceeds coding timeout")
    selected = profile["profile"]
    prompt = profile["prompt"]
    if not isinstance(selected, str) or not 1 <= len(selected) <= 256 or "\0" in selected:
        raise ValueError("Typed LSCli provider profile requires 1 to 256 characters")
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 131072
            or len(prompt.encode("utf-8")) > 131072):
        raise ValueError("Typed LSCli prompt requires 1 to 128 KiB of UTF-8")
    command = _resolve(paths["executable"], paths["runtime_root"])
    command += ["run", "--profile=" + selected, "--workspace", str(target_root),
                "--prompt-stdin", "--format", "jsonl"]
    for key in PATHS[1:]:
        command += ["--" + key.replace("_", "-"), str(paths[key])]
    for key in ("timeout_seconds", "request_limit", "tool_limit", "token_limit"):
        flag = "timeout" if key == "timeout_seconds" else key.replace("_", "-")
        command += ["--" + flag, str(limits[key])]
    return {"id": name + "-agent", "command": command, "policy_command": command,
            "allow_direct": False, "stdin_text": prompt,
            "timeout_seconds": limits["timeout_seconds"] + 20,
            "protocol_options": {"idle_timeout": limits["no_progress_seconds"],
                                 "output_limit": limits["output_limit_bytes"]},
            "launcher_info": {"client": "lscli", "launcher_mode": "lscli", "profile": name,
                              "prompt_transport": "stdin", "resolved_executable": command[0]}}
