"""Protocol and real process outcomes must agree."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/ls-codex-heartbeat-checks/scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


protocol = module("heartbeat_protocol")
process = module("heartbeat_process")
START = {"task": "task1", "session": "session1", "profile": "fixture"}
RESULT = {"status": "completed", "task": "task1", "session": "session1",
          "output": "private result", "checkpoint": "a"*64}


def event(sequence, kind, data):
    return (json.dumps(dict(schema_version=1, sequence=sequence, type=kind, data=data))+"\n").encode()


VALID = event(1, "start", START) + event(2, "result", RESULT)


def test_fragmented_completion_does_not_retain_output():
    receipt = protocol.Receipt()
    for byte in VALID:
        receipt.feed(bytes([byte]))
    value = receipt.finish(0)
    assert value["completed"] and value["checkpoint"] == "a"*64
    assert "private result" not in repr(vars(receipt)) + repr(value)


@pytest.mark.parametrize("raw", [
    event(1, "result", RESULT),
    event(1, "start", START)+event(3, "result", RESULT),
    event(1, "start", START)+event(2, "result", {**RESULT, "task": "another"}),
    VALID+event(3, "result", RESULT), VALID+b" ",
    VALID[:-1], b'{"schema_version":1,"schema_version":1}\n',
    event(True, "start", START), event(1, "unexpected", {}),
    event(1, "start", START)+event(2, "approval_request", {}),
])
def test_invalid_receipts_fail(raw):
    receipt = protocol.Receipt()
    with pytest.raises(ValueError):
        receipt.feed(raw)
        receipt.finish(0)


@pytest.mark.parametrize("raw,code,reason", [(VALID,0,None), (VALID,3,"protocol_failed"),
    (b"",0,"protocol_error"), (VALID+b"x",0,"protocol_error")])
def test_process_and_receipt_acceptance(tmp_path, raw, code, reason):
    result = process.execute([sys.executable, "-c",
        f"import os,sys; os.write(1,{raw!r}); os.write(2,b'private diagnostics'); sys.exit({code})"],
        cwd=tmp_path, timeout=3, receipt=protocol.Receipt())
    assert result["termination_reason"] == reason
    assert result["process_returncode"] == code or reason == "protocol_error"
    assert (result["returncode"] == 0) == (reason is None)
    assert result["stdout_tail"] == result["stderr_tail"] == ""


def test_stderr_does_not_reset_activity_deadline(tmp_path):
    result = process.execute([sys.executable, "-c",
        "import os,time\nwhile True: os.write(2,b'noise'); time.sleep(.01)"],
        cwd=tmp_path, timeout=3, idle_timeout=.15, receipt=protocol.Receipt())
    assert result["termination_reason"] == "no_progress_timeout"
    assert result["cleanup_reaped"] and result["stdout_tail"] == result["stderr_tail"] == ""


def test_early_failure_is_not_completion():
    receipt = protocol.Receipt()
    receipt.feed(event(1, "result", {"status": "invalid"}))
    assert not receipt.finish(0)["completed"]


def test_frame_limit_precedes_json_decode(monkeypatch):
    monkeypatch.setattr(protocol, "FRAME_LIMIT", 10)
    with pytest.raises(ValueError, match="frame limit"):
        protocol.Receipt().feed(b"x"*11)


@pytest.mark.parametrize("name", ["My Profile", "日本語", "p"*256])
def test_existing_profile_name_contract(name):
    receipt = protocol.Receipt()
    receipt.feed(event(1, "start", {**START, "profile": name})+event(2, "result", RESULT))
    assert receipt.finish(0)["completed"]
