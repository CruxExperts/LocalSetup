from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from ls.core.sdk_payload.artifacts import inspect_artifact
from ls.core.sdk_payload.sbom import SBOM_PATH, document, encode

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "vendor/lscli"
MANIFEST = json.loads((SOURCE / "manifest.json").read_text())


@pytest.mark.parametrize("kind", ["wheel", "tar", "sdist"])
def test_artifact_payload_and_sbom_roundtrip(tmp_path, kind):
    prefix = {"wheel": "ls/_sdk_payload/", "tar": "vendor/lscli/", "sdist": "example-1.0/vendor/lscli/"}[kind]
    path = tmp_path / ("example.whl" if kind == "wheel" else "example.tar")
    data = {prefix + n: (SOURCE / n).read_bytes() for n in ["manifest.json", *MANIFEST["files"]]}
    if kind == "wheel":
        with zipfile.ZipFile(path, "w") as z:
            for n, content in data.items():
                z.writestr(n, content)
            z.writestr(SBOM_PATH, encode(MANIFEST))
    else:
        with tarfile.open(path, "w") as t:
            for n, content in data.items():
                info = tarfile.TarInfo(n)
                info.size = len(content)
                t.addfile(info, io.BytesIO(content))
    assert inspect_artifact(path)["manifest"] == MANIFEST
    with pytest.raises(ValueError, match="differs"):
        inspect_artifact(path, expected_digest="0" * 64)


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra", "duplicate", "traversal", "symlink", "sbom", "omitted"])
def test_rejects_untrusted_wheel_inventory(tmp_path, mutation):
    path = tmp_path / "bad.whl"
    with zipfile.ZipFile(path, "w") as z:
        for name in ["manifest.json", *MANIFEST["files"]]:
            if mutation == "omitted" or (mutation == "missing" and name == "pydantic_ai/__init__.py"):
                continue
            content = (SOURCE / name).read_bytes()
            if mutation == "changed" and name == "pydantic_ai/__init__.py":
                content = b"changed"
            z.writestr("ls/_sdk_payload/" + name, content)
        sbom = document(MANIFEST)
        if mutation == "sbom":
            sbom["components"][0]["licenses"] = []
        z.writestr(SBOM_PATH, json.dumps(sbom))
        if mutation in {"extra", "duplicate", "traversal", "symlink"}:
            name = {"extra": "extra.py", "duplicate": "manifest.json", "traversal": "../escape", "symlink": "link"}[mutation]
            info = zipfile.ZipInfo("ls/_sdk_payload/" + name)
            if mutation == "symlink":
                info.external_attr = 0o120777 << 16
            z.writestr(info, b"bad")
    with pytest.raises(ValueError):
        inspect_artifact(path)


def test_public_sbom_checks_full_vendor_metadata_and_manifest_binding(tmp_path):
    import hashlib
    from ls.core.package import verify_cyclonedx_sbom, write_cyclonedx_sbom

    path = tmp_path / "public.tar"
    with tarfile.open(path, "w") as archive:
        for name in ["manifest.json", *MANIFEST["files"]]:
            content = (SOURCE / name).read_bytes()
            member = tarfile.TarInfo("vendor/lscli/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        content = (ROOT / "VERSION").read_bytes()
        member = tarfile.TarInfo("VERSION")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
        content = (ROOT / "uv.lock").read_bytes()
        member = tarfile.TarInfo("uv.lock")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    metadata = {"pack_id": "localsetup", "version": "fixture", "source_commit": "fixture",
                "sdk_manifest_sha256": hashlib.sha256((SOURCE / "manifest.json").read_bytes()).hexdigest()}
    sbom = write_cyclonedx_sbom(ROOT, path, metadata)
    assert verify_cyclonedx_sbom(sbom, path, metadata)["ok"]
    data = json.loads(sbom.read_text())
    vendor = next(c for c in data["components"] if c.get("bom-ref", "").endswith(":pydantic-ai-slim"))
    vendor["licenses"] = []
    sbom.write_text(json.dumps(data))
    assert not verify_cyclonedx_sbom(sbom, path, metadata)["ok"]
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w"):
        pass
    with pytest.raises(ValueError, match="missing"):
        inspect_artifact(empty, required=False, expected_digest=metadata["sdk_manifest_sha256"])


@pytest.mark.parametrize("bad_name,link", [("vendor/./lscli/pydantic_ai/__init__.py", False),
                                          ("vendor/other/../lscli/pydantic_ai/__init__.py", False),
                                          ("vendor", True), ("wrapper", True)])
def test_tar_alias_and_ancestor_cannot_redirect_verified_payload(tmp_path, bad_name, link):
    path = tmp_path / "bad.tar"
    prefix = "wrapper/" if bad_name == "wrapper" else ""
    with tarfile.open(path, "w") as archive:
        for name in ["manifest.json", *MANIFEST["files"]]:
            content = (SOURCE / name).read_bytes()
            member = tarfile.TarInfo(prefix + "vendor/lscli/" + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo(bad_name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = "elsewhere"
            archive.addfile(member)
        else:
            member.size = 3
            archive.addfile(member, io.BytesIO(b"bad"))
    with pytest.raises(ValueError):
        inspect_artifact(path)
