"""Mutable user bins must never resolve bootstrap commands as root."""
from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from hermes_cli.service_manager import S6ServiceManager


@pytest.mark.parametrize("keep_env", ["0", "1"])
@pytest.mark.parametrize("caller", ["main", "shim", "dashboard", "reconcile", "generated", "nonroot-shim", "stage2"])
def test_user_bins_only_after_drop(built_image, caller, keep_env):
    # Disposable reduced/full image; no live gateway or host home mounts. Shadow
    # stubs are harmless, and root only LOOKS UP their names, never executes them.
    home = "/srv/custom home;literal"
    setup = r'''
set -eu
export HERMES_HOME='/srv/custom home;literal'
mkdir -p "$HERMES_HOME/.local/bin" /opt/hermes/.venv/bin /run/service
# A mutable shell is selected by an unqualified with-contenv shebang before
# runtime-env.sh has a chance to sanitize PATH. Exit rather than run any payload.
printf '#!/bin/sh\necho MUTABLE_SHELL_SELECTED >&2\nexit 92\n' > "$HERMES_HOME/.local/bin/sh"
chmod 755 "$HERMES_HOME/.local/bin/sh"
for name in usermod chown s6-setuidgid; do
    printf '#!/bin/sh\nexit 0\n' > "$HERMES_HOME/.local/bin/$name"
    chmod 755 "$HERMES_HOME/.local/bin/$name"
done
printf '#!/bin/sh\n/usr/bin/id -u\n' > "$HERMES_HOME/.local/bin/custom-app"
chmod 755 "$HERMES_HOME/.local/bin/custom-app"
chown -R hermes:hermes "$HERMES_HOME"
# Even an inherited PATH containing mutable data must be removed for root.
export PATH="$HERMES_HOME/.local/bin:$PATH"
. /opt/hermes/docker/runtime-env.sh
for name in usermod chown s6-setuidgid; do
    resolved=$(command -v "$name")
    case "$resolved" in "$HERMES_HOME"/*) exit 91;; esac
    printf 'trusted:%s=%s\n' "$name" "$resolved"
done
'''
    probe = (
        "import json,os,subprocess; "
        "print(json.dumps(dict(uid=os.getuid(),home=os.environ['HOME'],"
        "path=os.environ['PATH'],custom=subprocess.check_output(['custom-app'],text=True).strip())))"
    )
    # Replacing only the app endpoint lets dashboard/reconciler/generated launch
    # boundaries run intact without starting a gateway or importing its state.
    app = "#!/usr/local/bin/python3\n" + probe + "\n"
    setup += "printf '%s' " + shlex.quote(app) + " > /opt/hermes/.venv/bin/hermes\n"
    setup += "printf '%s' " + shlex.quote(app) + " > /opt/hermes/.venv/bin/python\n"
    setup += "chmod 755 /opt/hermes/.venv/bin/hermes /opt/hermes/.venv/bin/python\n"
    if caller == "main":
        setup += "printf '%s' " + shlex.quote(app) + " > \"$HERMES_HOME/.local/bin/user-probe\"\n"
        setup += 'chmod 755 "$HERMES_HOME/.local/bin/user-probe"\n'
        launch = "/opt/hermes/docker/main-wrapper.sh user-probe"
    elif caller == "stage2":
        launch = "/opt/hermes/docker/stage2-hook.sh"
    elif caller == "shim":
        launch = "/opt/hermes/bin/hermes"
    elif caller == "nonroot-shim":
        launch = "/command/s6-setuidgid hermes /opt/hermes/bin/hermes"
    elif caller == "dashboard":
        launch = "HERMES_DASHBOARD=1 /opt/hermes/docker/s6-rc.d/dashboard/run"
    elif caller == "reconcile":
        launch = "/opt/hermes/docker/cont-init.d/02-reconcile-profiles"
    else:
        run = S6ServiceManager._render_run_script("coder", {"HOME": ""})
        setup += "printf '%s' " + shlex.quote(run) + " > /tmp/generated-run\n"
        setup += "chmod 755 /tmp/generated-run\n"
        launch = "/tmp/generated-run"
    # Test both inherited-env and normal envdir restoration with mutable PATH.
    setup += 'export PATH="$HERMES_HOME/.local/bin:/command:$PATH"\n'
    setup += f"export S6_KEEP_ENV={keep_env}\n"
    setup += 'mkdir -p /run/s6/container_environment\n'
    setup += 'for name in PATH HERMES_HOME HOME HERMES_DASHBOARD; do\n'
    setup += '  eval "value=\\${$name-}"; printf "%s" "$value" > "/run/s6/container_environment/$name"\n'
    setup += 'done\n'
    if caller == "dashboard":
        setup += 'printf 1 > /run/s6/container_environment/HERMES_DASHBOARD\n'
    result = subprocess.run(
        ["docker", "run", "--rm", "--pids-limit=-1", "--entrypoint", "/bin/sh", built_image, "-c", setup + launch],
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("trusted:") == 3
    state = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{"uid"')))
    assert state["uid"] == 10000
    assert state["custom"] == "10000"
    assert state["home"] == ("" if caller == "generated" else home)
    assert f"{home}/.local/bin" in state["path"].split(":")


def test_root_diagnostic_opt_out_preserves_environment(built_image):
    script = r'''
set -eu
printf '#!/bin/sh\n/usr/bin/id -u\nprintf "%%s\\n" "$HOME" "$PATH"\n' > /opt/hermes/.venv/bin/hermes
chmod 755 /opt/hermes/.venv/bin/hermes
export HOME=/root/diagnostic PATH=/usr/bin:/bin
export HERMES_HOME='/srv/other home' HERMES_DOCKER_EXEC_AS_ROOT=1
exec /opt/hermes/bin/hermes
'''
    result = subprocess.run(
        ["docker", "run", "--rm", "--pids-limit=-1", "--entrypoint", "/bin/sh", built_image, "-c", script],
        text=True, capture_output=True, timeout=30, check=True,
    )
    assert result.stdout.splitlines() == ["0", "/root/diagnostic", "/usr/bin:/bin"]

@pytest.mark.parametrize("caller", ["setup", "permissions", "main-service", "dashboard-finish", "generated-finish", "generated-log"])
def test_remaining_root_with_contenv_wrappers(built_image, caller):
    # Harmless command shadows catch pre-helper root resolution. Log input ends
    # at EOF; the inert main-service sleep endpoint is replaced by a UID probe.
    setup = r'''
set -eu
export HERMES_HOME=/srv/wrapper-state S6_KEEP_ENV=1
mkdir -p "$HERMES_HOME/.local/bin" "$HERMES_HOME/logs/gateways" /run/s6-rc/servicedirs/example/supervise
for name in sh id chown chmod basename mkdir rm s6-setuidgid s6-log sleep; do
  case "$name" in s6-*) trusted=/command/$name;; *) trusted=/usr/bin/$name;; esac
  printf '#!/bin/sh\nif [ "$(/usr/bin/id -u)" = 0 ]; then echo MUTABLE_ROOT_COMMAND >&2; exit 92; fi\nexec %s "$@"\n' "$trusted" > "$HERMES_HOME/.local/bin/$name"
  chmod 755 "$HERMES_HOME/.local/bin/$name"
done
chown -R hermes:hermes "$HERMES_HOME"
'''
    expected = 0
    if caller == "setup":
        launch = "/etc/cont-init.d/01-hermes-setup"
    elif caller == "permissions":
        launch = "/opt/hermes/docker/cont-init.d/015-supervise-perms"
    elif caller == "main-service":
        setup += "printf '#!/bin/sh\\n/usr/bin/id -u\\n' > /bin/sleep\nchmod 755 /bin/sleep\n"
        launch = "/opt/hermes/docker/s6-rc.d/main-hermes/run"
    elif caller == "dashboard-finish":
        setup += "unset HERMES_DASHBOARD\n"
        launch = "/opt/hermes/docker/s6-rc.d/dashboard/finish 0"
        expected = 125
    else:
        script = (S6ServiceManager._render_finish_script() if caller == "generated-finish"
                  else S6ServiceManager._render_log_run("coder"))
        setup += "printf '%s' " + shlex.quote(script) + " > /tmp/wrapper\nchmod 755 /tmp/wrapper\n"
        launch = "/tmp/wrapper 0" if caller == "generated-finish" else "printf 'log probe\\n' | /tmp/wrapper"
        if caller == "generated-finish":
            expected = 125
    setup += 'export PATH="$HERMES_HOME/.local/bin:/command:$PATH"\n'
    result = subprocess.run(
        ["docker", "run", "--rm", "--pids-limit=-1", "--entrypoint", "/bin/sh", built_image, "-c", setup + launch],
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "MUTABLE_ROOT_COMMAND" not in result.stderr
    if caller == "main-service":
        assert result.stdout.strip() == "0"
