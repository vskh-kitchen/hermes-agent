#!/bin/sh
# shellcheck shell=sh
# Source after with-contenv (if used): image ENV expansion is build-time only.
export HERMES_HOME="${HERMES_HOME:-/opt/data}"
export HOME="$HERMES_HOME"
# Explicit empty disables the safe-root restriction; dashboard empty selects its
# Python policy (legacy /opt/data stays locked, other homes allow path changes).
export HERMES_WRITE_SAFE_ROOT="${HERMES_WRITE_SAFE_ROOT-$HERMES_HOME}"
export HERMES_DASHBOARD_FILES_ROOT="${HERMES_DASHBOARD_FILES_ROOT-$HERMES_HOME}"
# Bootstrap must never resolve commands from mutable home data (including an
# inherited PATH). Use only image-owned directories until privileges are gone.
if [ "$(/usr/bin/id -u)" = 0 ]; then
    export PATH="/command:/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
else
    # Keep the privilege-drop shim ahead of user-installed commands.
    export PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:$HERMES_HOME/.local/bin:$PATH"
fi
