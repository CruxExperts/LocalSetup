import importlib.util
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "ls" / "skills" / "ls-keepass-secret-references" / "scripts" / "localsetup_secrets.py"

spec = importlib.util.spec_from_file_location("localsetup_secrets", CLI)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_secret_id_line_reference() -> None:
    parsed = module.parse_reference("Secret ID: mail.box03.example.admin")
    assert parsed["id"] == "mail.box03.example.admin"
    assert parsed["field"] == "password"


def test_template_reference() -> None:
    parsed = module.parse_reference("{{secret:mail.box03.example.admin:username}}")
    assert parsed["type"] == "template"
    assert parsed["field"] == "username"


def test_uri_reference() -> None:
    parsed = module.parse_reference("secret://localsetup/repo/default/mail.box03.example.admin#field=url")
    assert parsed["scope"] == "repo"
    assert parsed["profile"] == "default"
    assert parsed["id"] == "mail.box03.example.admin"
    assert parsed["field"] == "url"
