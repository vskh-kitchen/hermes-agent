"""Relocated Docker homes must reach real subprocesses, not just config readers."""
from __future__ import annotations

import json
import subprocess

import pytest


@pytest.mark.parametrize("home", ["/opt/data", "/srv/hermes state"])
@pytest.mark.parametrize("safe", [None, ""])
def test_container_home_reaches_subprocess(built_image, home, safe):
    probe = (
        "import json,os,pwd,subprocess; from pathlib import Path; "
        "from agent.file_safety import is_write_denied; "
        "home=Path(os.environ['HERMES_HOME']); "
        "target=home/'home-probe'; target.write_text('ok'); "
        "assert not is_write_denied(str(target)); "
        "assert is_write_denied('/tmp/outside-home-probe') == bool(os.environ['HERMES_WRITE_SAFE_ROOT']); "
        "child=subprocess.check_output(['python','-c',"
        "'import os; print(os.path.expanduser(\"~\"))'],text=True).strip(); "
        "print(json.dumps(dict(home=os.environ['HOME'],child=child,"
        "passwd=pwd.getpwuid(os.getuid()).pw_dir,uid=os.getuid(),"
        "cwd=os.getcwd(),safe=os.environ['HERMES_WRITE_SAFE_ROOT'],"
        "files=os.environ['HERMES_DASHBOARD_FILES_ROOT'],path=os.environ['PATH'])))"
    )
    overrides = [] if safe is None else ["-e", "HERMES_WRITE_SAFE_ROOT=", "-e", "HERMES_DASHBOARD_FILES_ROOT="]
    result = subprocess.run(
        ["docker", "run", "--rm", "--pids-limit=-1", *overrides, "-e", f"HERMES_HOME={home}",
         "-e", "HERMES_UID=12000", "-e", "HERMES_GID=12000", "-w", "/tmp",
         built_image, "python", "-c", probe],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{"home"')))
    assert state["home"] == state["child"] == home
    assert state["passwd"] == "/nonexistent"
    assert state["safe"] == state["files"] == (home if safe is None else safe)
    assert state["uid"] == 12000
    assert state["cwd"] == "/tmp"
    assert f"{home}/.local/bin" in state["path"].split(":")
