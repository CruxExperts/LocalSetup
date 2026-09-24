"""Protected key generation through the shared OpenPGP lifecycle."""
from __future__ import annotations
from pathlib import Path
import subprocess
from ls.core.openpgp import (KeyIdentity, RSA_4096_TWO_YEAR_PROFILE,
                             SecretReference, SecretResolver, authorize_outbound,
                             generate_key, inspect_key, normalize_fingerprint)

def generate_keypair_gnupg(output_dir: Path, *, name: str, email: str,
                           home: Path, secret_provider: str, secret_name: str) -> tuple[Path, str]:
    output_dir = Path(output_dir)
    public_path = output_dir / "agentq.pub.asc"
    if public_path.exists():
        raise FileExistsError("public certificate output already exists")
    reference = SecretReference(secret_provider, secret_name)
    result = generate_key(identity=KeyIdentity(name, email), profile=RSA_4096_TWO_YEAR_PROFILE,
                          passphrase_reference=reference, secret_resolver=SecretResolver(), keyring_home=home)
    output_dir.mkdir(parents=True, exist_ok=True)
    with public_path.open("xb") as stream:
        stream.write(result.public_certificate)
    return public_path, result.primary_fingerprint

def fingerprint_for_certificate(path: Path) -> str:
    if Path(path).stat().st_size > 1024 * 1024:
        raise ValueError("certificate too large")
    return inspect_key(certificate=Path(path).read_bytes()).primary_fingerprint

def import_peer_certificate(registry: dict, peer_id: str) -> str:
    """Import only the exact public certificate selected by private authority."""
    from .registry import peer
    cfg = peer(registry, peer_id)
    authority = authorize_outbound(cfg["trust_state"], expected_scope=cfg["expected_scope"], role=cfg["role"])
    if authority.fingerprint != cfg["fingerprint"]:
        raise ValueError("peer pin mismatch")
    if inspect_key(certificate=authority.certificate).primary_fingerprint != authority.fingerprint:
        raise ValueError("peer certificate mismatch")
    result = subprocess.run(["gpg", "--homedir", str(registry["local"]["gnupg_home"]),
        "--batch", "--no-options", "--no-auto-key-retrieve", "--import"],
        input=authority.certificate, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("public certificate import failed")
    return authority.fingerprint

def export_public_certificate(registry: dict, output: Path, fingerprint: str) -> str:
    """Export one full-pin public certificate; no secret export exists."""
    selected = normalize_fingerprint(fingerprint)
    if selected != registry["local"]["fingerprint"]:
        raise ValueError("only local full fingerprint may be exported")
    result = subprocess.run(["gpg", "--homedir", str(registry["local"]["gnupg_home"]),
        "--batch", "--no-options", "--armor", "--export", selected],
        capture_output=True, timeout=30)
    if result.returncode or not result.stdout:
        raise RuntimeError("public certificate export failed")
    if inspect_key(certificate=result.stdout).primary_fingerprint != selected:
        raise ValueError("exported certificate mismatch")
    with Path(output).open("xb") as stream:
        stream.write(result.stdout)
    return selected
