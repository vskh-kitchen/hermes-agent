"""Focused contracts for runtime home defaults and generated s6 services."""
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
@pytest.mark.parametrize("home", [None, "/srv/custom home;literal"])
@pytest.mark.parametrize("safe", [None, "", "/srv/restricted"])
def test_runtime_env_defaults_and_explicit_roots(home, safe):
    env = {"PATH": os.defpath, "HOME": "/root"}
    if home is not None:
        env["HERMES_HOME"] = home
    if safe is not None:
        env["HERMES_WRITE_SAFE_ROOT"] = safe
        env["HERMES_DASHBOARD_FILES_ROOT"] = safe
    command = '. "$1"; printf "%s\\n" "$HERMES_HOME" "$HOME" "$HERMES_WRITE_SAFE_ROOT" "$HERMES_DASHBOARD_FILES_ROOT" "$PATH"'
    result = subprocess.run(["sh", "-c", command, "sh", str(ROOT / "docker/runtime-env.sh")],
                            env=env, capture_output=True, text=True, timeout=10, check=True)
    actual_home, user_home, actual_safe, files, path = result.stdout.splitlines()
    expected = home or "/opt/data"
    assert actual_home == expected
    assert user_home == "/root"  # The helper leaves HOME and command resolution to callers.
    assert path == os.defpath
    assert actual_safe == files == (expected if safe is None else safe)


@pytest.mark.platforms("posix")
@pytest.mark.spawns_gateway_lookalike
@pytest.mark.parametrize("profile", ["default", "coder"])
@pytest.mark.parametrize("extra", [{}, {"HOME": "/srv/explicit home"}, {"HOME": ""},
                                    {"HERMES_WRITE_SAFE_ROOT": "", "HERMES_DASHBOARD_FILES_ROOT": ""},
                                    {"HERMES_WRITE_SAFE_ROOT": "/srv/restricted", "HERMES_DASHBOARD_FILES_ROOT": "/srv/files"}])
def test_s6_run_uses_runtime_home(tmp_path, profile, extra):
    home = tmp_path / "custom home;literal"
    home.mkdir()
    # Some pytest temp filesystems are noexec; executable stubs live in the checkout.
    with tempfile.TemporaryDirectory(dir=ROOT) as executables:
        bin_dir = Path(executables)
        (bin_dir / "id").write_text("#!/bin/sh\necho 10000\n")
        (bin_dir / "hermes").write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            "print(json.dumps(dict(home=os.environ['HOME'], cwd=os.getcwd(), "
            "safe=os.environ['HERMES_WRITE_SAFE_ROOT'], files=os.environ['HERMES_DASHBOARD_FILES_ROOT'], "
            "args=sys.argv[1:])))\n"
        )
        for executable in bin_dir.iterdir():
            executable.chmod(0o755)
        script = S6ServiceManager._render_run_script(profile, extra).replace(
            "/opt/hermes/docker/runtime-env.sh", shlex.quote(str(ROOT / "docker/runtime-env.sh")))
        env = dict(os.environ, HERMES_HOME=str(home), HOME="/root", PATH=f"{bin_dir}:{os.defpath}")
        for key in ("HERMES_WRITE_SAFE_ROOT", "HERMES_DASHBOARD_FILES_ROOT"):
            env.pop(key, None)
        result = subprocess.run(["sh", "-c", script], env=env, text=True,
                                capture_output=True, timeout=10, check=True)
    state = json.loads(result.stdout)
    assert state["cwd"] == str(home)
    assert state["home"] == extra.get("HOME", str(home))
    assert state["safe"] == extra.get("HERMES_WRITE_SAFE_ROOT", str(home))
    assert state["files"] == extra.get("HERMES_DASHBOARD_FILES_ROOT", str(home))
    expected = ["gateway", "run", "--replace"]
    assert state["args"] == (expected if profile == "default" else ["-p", profile, *expected])
