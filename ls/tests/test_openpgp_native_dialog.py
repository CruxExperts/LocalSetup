"""A real primary tmux viewer hands private input to real OpenPGP consumers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from ls.core.application_profiles.service import resolve_profile
from ls.core.openpgp import (
    EnvelopePolicy, KeyCapability, KeyIdentity, LocalTrust, RSA_4096_TWO_YEAR_PROFILE,
    SecretProvider, SecretReference, SecretResolver, enroll_key, generate_key,
    inspect_key, open_envelope, seal_envelope,
)
from ls.core.openpgp.native_input import (
    NativeInputContext, describe_enrollment_operation, describe_open_operation,
    describe_seal_operation,
)
from ls.core.terminal_ops.command_backend import TmuxCommandBackend
from ls.core.terminal_ops.command_service import ShellCommandService
from ls.core.terminal_ops.command_state import ShellCommandError, make_owner_binding, process_info, process_state


@contextmanager
def _private_fixture():
    """Keep failed fixture state available for exact owned cleanup diagnosis."""
    work = Path(tempfile.mkdtemp(prefix="ls-pgp-ui-"))
    yield work
    shutil.rmtree(work)


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("gpg")
                    or not Path("/proc/self/stat").exists(), reason="native Linux tmux and GnuPG are required")
def test_native_passphrase_round_trip_and_certificate_enrollment() -> None:
    root = Path(__file__).resolve().parents[2]
    with _private_fixture() as work:
        backend = TmuxCommandBackend(config_file="/dev/null", socket_root=work / "sockets")
        if backend.capability().get("version") != "tmux 3.4":
            pytest.skip("native private forms require tmux 3.4")
        service = ShellCommandService(work / "state", backend=backend, source_root=root)
        owner = make_owner_binding(owner_pid=os.getpid(), agent_id="openpgp-native-test")
        profile = resolve_profile(source_root=root, home=work / "home")
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"TMUX", "TMUX_PANE"} and not key.endswith("_SUDO_KEY")}
        environment["TERM"] = "xterm-256color"
        outer, home = work / "viewer.sock", work / "key-home"
        canary = "NativeOpenPGPCanary9"
        resolver = SecretResolver(env_values={"FIXTURE_PASSPHRASE": canary})
        handle = None
        runner = None
        viewer_server = None
        viewers = []

        def tmux(*args):
            return subprocess.run(["tmux", "-f", "/dev/null", "-S", str(outer), *args],
                                  env=environment, capture_output=True, text=True, timeout=5, check=True).stdout

        def eventually(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if value := predicate():
                    return value
                time.sleep(.05)
            raise AssertionError("Native OpenPGP viewer did not reach the expected state.")

        def call(method, *args, **kwargs):
            deadline = time.monotonic() + 7
            while True:
                try:
                    return method(*args, **kwargs)
                except ShellCommandError as exc:
                    if exc.code != "busy" or exc.effect != "none" or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)

        def exited(identity):
            current = process_info(identity["pid"])
            return (current is None or current["starttime"] != identity["starttime"]
                    or process_state(identity["pid"]) in {None, "Z", "X", "x"})

        try:
            generated = generate_key(
                identity=KeyIdentity("Native OpenPGP Fixture", "native@example.invalid"),
                profile=RSA_4096_TWO_YEAR_PROFILE,
                passphrase_reference=SecretReference(SecretProvider.ENV, "FIXTURE_PASSPHRASE"),
                secret_resolver=resolver, keyring_home=home,
            )
            fingerprint = generated.primary_fingerprint
            inspection = inspect_key(certificate=generated.public_certificate)
            created = call(service.ensure, cwd=work, settings=profile["settings"],
                           settings_digest=profile["digest"], owner=owner,
                           profile_name=profile["name"], profile_base=profile["base_profile"],
                           layout_mode="canvas-v3")
            handle = created["handle"]
            attach = [sys.executable, str(root / "ls/tools/localsetup.py"), "--source-root", str(root),
                      "shell", "--state-root", str(work / "state"), "attach", "--session", created["session_alias"]]
            pane = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "openpgp",
                        "-x", "120", "-y", "40", shlex.join(attach)).strip()
            viewer_server = process_info(int(tmux("display-message", "-p", "#{pid}").strip()))
            assert viewer_server is not None
            tmux("set-option", "-g", "status", "off")
            eventually(lambda: "Workspace ready" in tmux("capture-pane", "-p", "-t", pane))

            def primary_ready():
                directory, session, _ = service._load(handle)
                pins, unknown = service._pins(directory, remove_dead=False)
                primary = [pin for pin in pins if pin.get("primary_viewport") is True]
                return (not unknown and len(primary) == 1 and
                        service._native_primary_client(directory, session, primary[0].get("viewer_pid")))

            eventually(primary_ready)
            directory, _, _ = service._load(handle)
            pins, unknown = service._pins(directory, remove_dead=False)
            assert not unknown
            viewers = [process_info(pin["pid"]) for pin in pins]
            assert viewers and all(item is not None and item["pid"] != os.getpid() for item in viewers)

            def perform(title, descriptor, value, consume):
                nonlocal runner
                result = []
                context = NativeInputContext(service=service, handle=handle, owner=owner,
                                             operation_id=descriptor.purpose, expected=descriptor,
                                             deadline=time.monotonic() + 25, validate_current=lambda: True)

                def invoke():
                    try:
                        result.append(consume(context))
                    except BaseException as error:
                        result.append(error)

                runner = threading.Thread(target=invoke)
                runner.start()
                try:
                    eventually(lambda: title in tmux("capture-pane", "-p", "-t", pane))
                except AssertionError:
                    raise AssertionError({"title": title, "result_types": [type(item).__name__ for item in result],
                                          "codes": [str(getattr(item, "code", "")) for item in result]}) from None
                tmux("send-keys", "-t", pane, "-l", value)
                if value == canary:
                    assert canary not in tmux("capture-pane", "-p", "-t", pane)
                tmux("send-keys", "-t", pane, "C-s")
                runner.join(timeout=30)
                assert not runner.is_alive() and len(result) == 1
                assert not isinstance(result[0], BaseException), type(result[0]).__name__
                assert service._load(handle)[1]["native_layout"].get("overlay_lock") is None
                return result[0]

            enrollment = dict(inspection=inspection, expected_fingerprint=fingerprint,
                              required_capabilities=(KeyCapability.SIGN, KeyCapability.ENCRYPT),
                              profile=RSA_4096_TWO_YEAR_PROFILE, local_trust=LocalTrust(frozenset()))
            enrolled = perform("Enroll certificate", describe_enrollment_operation(**enrollment), fingerprint,
                               lambda context: enroll_key(**enrollment, native_input=context))
            assert enrolled.local_trust.fingerprints == frozenset({fingerprint})
            payload = b"exact native-dialog payload\x00\xff"
            seal = dict(payload=payload, signer_fingerprint=fingerprint,
                        recipient_fingerprints=(fingerprint,), local_trust=enrolled.local_trust, gnupg_home=home)
            ciphertext = perform("Unlock signing key", describe_seal_operation(**seal), canary,
                                 lambda context: seal_envelope(**seal, passphrase_reference=None,
                                                               secret_resolver=resolver, native_input=context))
            opening = dict(serialized=ciphertext, gnupg_home=home, local_trust=enrolled.local_trust,
                           policy=EnvelopePolicy(expected_signers=(fingerprint,), expected_recipients=(fingerprint,)))
            plaintext = perform("Unlock encrypted content", describe_open_operation(**opening), canary,
                                lambda context: open_envelope(**opening, secret_resolver=resolver, native_input=context))
            assert plaintext == payload
            assert canary not in tmux("capture-pane", "-p", "-S", "-", "-t", pane)
            for path in work.rglob("*"):
                if stat.S_ISREG(path.lstat().st_mode):
                    assert canary.encode() not in path.read_bytes(), path.name
        finally:
            try:
                if runner is not None:
                    runner.join(timeout=35)
                    assert not runner.is_alive()
            finally:
                try:
                    if viewer_server is not None:
                        identity = process_info(viewer_server["pid"])
                        if identity is not None and identity["starttime"] == viewer_server["starttime"]:
                            tmux("kill-server")
                        eventually(lambda: exited(viewer_server))
                    eventually(lambda: all(exited(item) for item in viewers))
                finally:
                    try:
                        if handle is not None:
                            eventually(lambda: call(service.close, handle, owner=owner).get("cleanup_state") == "removed")
                    finally:
                        if home.exists():
                            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                                           capture_output=True, check=True, timeout=5)
