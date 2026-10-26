"""The kv example end to end, as unified-gitops runs it: describe, apply, list."""

import json
import os
import subprocess
import sys
from pathlib import Path

KV = Path(__file__).resolve().parent.parent / "kv.py"


def run(store, *args, stdin=None, write=False):
    env = {
        "PATH": os.environ["PATH"],
        "APPLICATION_CONFIG": json.dumps({"dir": str(store)}),
    }
    if write:
        env["UNIFIED_ALLOW_WRITE"] = "1"
    return subprocess.run(
        [sys.executable, str(KV), *args],
        input=stdin,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )


def test_describe_orders_entries_after_namespaces(tmp_path):
    out = run(tmp_path, "describe")
    assert out.returncode == 0
    kinds = {k["kind"]: k for k in json.loads(out.stdout)["kinds"]}
    assert kinds["entry"]["depends_on"] == ["namespace"]


def test_apply_then_list(tmp_path):
    create = json.dumps({"op": "create", "object": {"name": "team-a"}})
    assert (
        run(
            tmp_path, "apply", "--kind", "namespace", stdin=create, write=True
        ).returncode
        == 0
    )
    out = run(tmp_path, "list", "--kind", "namespace")
    assert json.loads(out.stdout) == [{"name": "team-a"}]


def test_apply_refuses_without_the_write_flag(tmp_path):
    create = json.dumps({"op": "create", "object": {"name": "team-a"}})
    out = run(tmp_path, "apply", "--kind", "namespace", stdin=create)
    assert out.returncode != 0 and "UNIFIED_ALLOW_WRITE" in out.stderr
