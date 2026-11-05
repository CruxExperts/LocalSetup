"""Setuptools mapping from the single canonical source to private wheel data."""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import shutil
import stat

from setuptools.command.build_py import build_py

# Setuptools loads cmdclass files before the source package is importable.
# Load the sibling standard-library verifier directly, without runtime imports.
_spec = spec_from_file_location("_localsetup_sdk_build_integrity", Path(__file__).with_name("integrity.py"))
if _spec is None or _spec.loader is None:
    raise RuntimeError("Cannot load SDK build verifier")
_integrity = module_from_spec(_spec)
_spec.loader.exec_module(_integrity)
verify = _integrity.verify
_sbom_spec = spec_from_file_location("_localsetup_sdk_build_sbom", Path(__file__).with_name("sbom.py"))
if _sbom_spec is None or _sbom_spec.loader is None:
    raise RuntimeError("Cannot load SDK SBOM builder")
_sbom = module_from_spec(_sbom_spec)
_sbom_spec.loader.exec_module(_sbom)

_dependency_spec = spec_from_file_location("_localsetup_sdk_dependency_integrity", Path(__file__).with_name("dependency_integrity.py"))
if _dependency_spec is None or _dependency_spec.loader is None:
    raise RuntimeError("Cannot load dependency lock verifier")
_dependencies = module_from_spec(_dependency_spec)
_dependency_spec.loader.exec_module(_dependencies)


class BuildSDK(build_py):
    """Copy only validated SDK files; never install their public import names."""

    def run(self) -> None:
        if self.editable_mode:
            super().run()
            return
        source = Path(__file__).resolve().parents[3] / "vendor" / "lscli"
        _dependencies.verify(source.parents[1])
        manifest = verify(source)
        build_root = Path(self.build_lib).absolute()
        destination = build_root / "ls" / "_sdk_payload"
        for path in (*build_root.parents, build_root, build_root / "ls", destination):
            if path.is_symlink():
                raise ValueError("SDK build destination must not contain symlinks")
        if destination.exists():
            previous = verify(destination)
            if previous != manifest:
                raise ValueError("Stale SDK build payload: use a fresh build output directory")
        config = build_root / "ls" / "config"
        if config.is_symlink() or (config.exists() and not config.is_dir()):
            raise ValueError("Dependency build directory must be a regular directory")
        dependency_data = {
            name: _dependencies.regular_bytes(source.parents[1] / "ls" / "config" / name)
            for name in (*_dependencies.LOCKS, _dependencies.RECEIPT)
        }
        for name, data in dependency_data.items():
            target = config / name
            if target.is_symlink() or (target.exists() and _dependencies.regular_bytes(target) != data):
                raise ValueError("Stale dependency build output: use a fresh build directory")
        tools = build_root / "ls" / "tools"
        askpass = tools / "sudo_askpass"
        if tools.is_symlink() or (tools.exists() and not tools.is_dir()) or askpass.is_symlink():
            raise ValueError("Askpass build destination must not contain symlinks")
        super().run()
        if tools.is_symlink() or (tools.exists() and not tools.is_dir()) or askpass.is_symlink():
            raise ValueError("Askpass build destination must not contain symlinks")
        if askpass.exists():
            tools_fd = os.open(tools, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fd = os.open("sudo_askpass", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=tools_fd)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Askpass build output must be a regular, unlinked file")
                    os.fchmod(fd, 0o755)
                finally:
                    os.close(fd)
            finally:
                os.close(tools_fd)
        config.mkdir(parents=True, exist_ok=True)
        for name, data in dependency_data.items():
            (config / name).write_bytes(data)
        _dependencies.verify(source.parents[1])
        for name, data in dependency_data.items():
            if _dependencies.regular_bytes(config / name) != data:
                raise ValueError("Dependency build output changed during copying")
        destination.mkdir(parents=True, exist_ok=True)
        for name in ["manifest.json", *sorted(manifest["files"])]:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        if verify(destination) != manifest:
            raise ValueError("SDK build payload changed during copying")
        sbom_path = build_root / _sbom.SBOM_PATH
        if sbom_path.is_symlink() or (sbom_path.exists() and not sbom_path.is_file()):
            raise ValueError("SDK SBOM destination must be a regular file")
        sbom_path.write_bytes(_sbom.encode(manifest))

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = super().get_outputs(include_bytecode=include_bytecode)
        destination = Path(self.build_lib) / "ls" / "_sdk_payload"
        if destination.is_dir():
            manifest = verify(destination)
            outputs.extend(str(destination / name) for name in ["manifest.json", *manifest["files"]])
            outputs.append(str(Path(self.build_lib) / _sbom.SBOM_PATH))
        return outputs
