"""Runtime homes and safety defaults must reach application subprocesses."""
from __future__ import annotations

import json
import subprocess

import pytest


@pytest.mark.parametrize("home", ["/opt/data", "/srv/hermes state"])
@pytest.mark.parametrize("safe", [None, "", "/srv/restricted"])
def test_container_home_reaches_subprocess(built_image, home, safe):
    probe = (
        "import json,os,subprocess; "
        "child=subprocess.check_output(['python','-c',"
        "'import os; print(os.path.expanduser(\"~\"))'],text=True).strip(); "
        "print(json.dumps(dict(home=os.environ['HOME'],child=child,"
        "cwd=os.getcwd(),safe=os.environ['HERMES_WRITE_SAFE_ROOT'],"
        "files=os.environ['HERMES_DASHBOARD_FILES_ROOT'],path=os.environ['PATH'])))"
    )
    overrides = [] if safe is None else ["-e", f"HERMES_WRITE_SAFE_ROOT={safe}",
                                        "-e", f"HERMES_DASHBOARD_FILES_ROOT={safe}"]
    result = subprocess.run(
        ["docker", "run", "--rm", "--pids-limit=-1", *overrides, "-e", f"HERMES_HOME={home}",
         "-w", "/tmp", built_image, "python", "-c", probe],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{"home"')))
    assert state["home"] == state["child"] == home
    assert state["safe"] == state["files"] == (home if safe is None else safe)
    assert state["cwd"] == "/tmp"
    path = state["path"].split(":")
    assert path.index(f"{home}/.local/bin") > path.index("/opt/hermes/bin")
