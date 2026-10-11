"""Behavior contracts for the container's runtime home and s6 launch paths."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

import pytest

from hermes_cli.service_manager import S6ServiceManager

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("profile", ["default", "coder"])
@pytest.mark.parametrize("extra", [{}, {"HERMES_WRITE_SAFE_ROOT": "", "HERMES_DASHBOARD_FILES_ROOT": ""}, {"HOME": "/srv/explicit user home"}, {"HOME": ""}])
def test_s6_run_uses_runtime_home(tmp_path, profile, extra):
    home = tmp_path / "custom home;literal"
    home.mkdir()
    # Test command stubs need an executable filesystem (pytest temp may be noexec).
    with tempfile.TemporaryDirectory(dir=ROOT) as executables:
        bin_dir = Path(executables)
        (bin_dir / "id").write_text("#!/bin/sh\necho 10000\n")
        (bin_dir / "s6-setuidgid").write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
        (bin_dir / "hermes").write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            "print(json.dumps(dict(home=os.environ.get('HOME'), "
            "hermes_home=os.environ.get('HERMES_HOME'), cwd=os.getcwd(), "
            "safe=os.environ.get('HERMES_WRITE_SAFE_ROOT'), "
            "files=os.environ.get('HERMES_DASHBOARD_FILES_ROOT'), "
            "path=os.environ['PATH'], args=sys.argv[1:])))\n"
        )
        for executable in bin_dir.iterdir():
            executable.chmod(0o755)
        run = tmp_path / "run"
        # Relocate only the installed helper; execute the generated script intact.
        script = S6ServiceManager._render_run_script(profile, extra).replace(
            "/opt/hermes/docker/runtime-env.sh", shlex.quote(str(ROOT / "docker/runtime-env.sh"))
        )
        script = script.replace("/opt/hermes/.venv/bin/hermes", shlex.quote(str(bin_dir / "hermes")))
        # Relocate the trusted identity executable for this non-root unit seam.
        script = script.replace("/usr/bin/id", shlex.quote(str(bin_dir / "id")))
        helper = (ROOT / "docker/runtime-env.sh").read_text().replace(
            "/usr/bin/id", shlex.quote(str(bin_dir / "id")))
        (bin_dir / "runtime-env.sh").write_text(helper)
        script = script.replace(shlex.quote(str(ROOT / "docker/runtime-env.sh")),
                                shlex.quote(str(bin_dir / "runtime-env.sh")))
        run.write_text(script)
        env = dict(os.environ, HERMES_HOME=str(home), HOME="/root",
                   PATH=f"{bin_dir}:{os.defpath}")
        env.pop("HERMES_WRITE_SAFE_ROOT", None)
        result = subprocess.run(["sh", str(run)], env=env, text=True,
                                capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["hermes_home"] == state["cwd"] == str(home)
    assert state["home"] == extra.get("HOME", str(home))
    assert state["safe"] == extra.get("HERMES_WRITE_SAFE_ROOT", str(home))
    assert state["files"] == extra.get("HERMES_DASHBOARD_FILES_ROOT", str(home))
    assert f"{home}/.local/bin" in state["path"].split(":")
    expected = ["gateway", "run", "--replace"]
    if profile != "default":
        expected = ["-p", profile, *expected]
    assert state["args"] == expected


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("home", [None, "/srv/custom home;literal"])
@pytest.mark.parametrize("safe", [None, "", "/srv/restricted:/srv/second"])
def test_runtime_env_defaults_and_explicit_roots(home, safe):
    env = {"PATH": os.defpath, "HOME": "/root"}
    if home is not None:
        env["HERMES_HOME"] = home
    if safe is not None:
        env["HERMES_WRITE_SAFE_ROOT"] = safe
        env["HERMES_DASHBOARD_FILES_ROOT"] = "/srv/files-only"
    command = '. "$1"; printf "%s\\n" "$HERMES_HOME" "$HOME" "$HERMES_WRITE_SAFE_ROOT" "$HERMES_DASHBOARD_FILES_ROOT" "$PATH"'
    result = subprocess.run(["sh", "-c", command, "sh", str(ROOT / "docker/runtime-env.sh")],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    actual_home, user_home, actual_safe, files, path = result.stdout.splitlines()
    expected = home or "/opt/data"
    assert actual_home == user_home == expected
    assert actual_safe == (expected if safe is None else safe)
    assert files == ("/srv/files-only" if safe is not None else expected)
    if os.geteuid() == 0:
        assert f"{expected}/.local/bin" not in path.split(":")
        assert path.split(":")[:3] == ["/command", "/opt/hermes/bin", "/opt/hermes/.venv/bin"]
    else:
        assert path.split(":")[:3] == ["/opt/hermes/bin", "/opt/hermes/.venv/bin", f"{expected}/.local/bin"]
