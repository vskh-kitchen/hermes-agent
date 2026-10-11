#!/bin/sh
# shellcheck shell=sh
# Source after with-contenv: image ENV expansion cannot follow runtime overrides.
export HERMES_HOME="${HERMES_HOME:-/opt/data}"
# Unset-only defaults preserve explicit empty roots and their existing semantics.
export HERMES_WRITE_SAFE_ROOT="${HERMES_WRITE_SAFE_ROOT-$HERMES_HOME}"
export HERMES_DASHBOARD_FILES_ROOT="${HERMES_DASHBOARD_FILES_ROOT-$HERMES_HOME}"
