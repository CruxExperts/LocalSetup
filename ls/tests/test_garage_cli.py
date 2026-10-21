from __future__ import annotations
import json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]; CLI=ROOT/"ls/skills/ls-garage-s3-admin/scripts/garage.py"
def run(*args): return subprocess.run([sys.executable,"-I","-S",str(CLI),*args],text=True,capture_output=True)
def test_offline_schema_and_plan():
    assert run("--schema","request").returncode==0
    out=run("--tool","s3.ListBuckets","--args-json","{}")
    assert out.returncode==0 and json.loads(out.stdout)["outcome"]=="planned"
