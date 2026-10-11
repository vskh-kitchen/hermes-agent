"""UID remaps must only adopt Hermes-owned state, including on repeated boot."""
from __future__ import annotations

import json
import shlex
import subprocess

import pytest


@pytest.mark.parametrize("home", ["/srv/custom state", "/opt/data"])
def test_repeated_stage2_uid_remap_preserves_unrelated_files(built_image, home):
    # No running gateway: execute two complete bootstrap passes sequentially in
    # one disposable container so passwd changes persist between passes.
    script = r'''
set -eu
export PATH="/command:$PATH"
export HERMES_HOME='/srv/custom state'
export HERMES_UID=12000 HERMES_GID=12000
export API_SERVER_KEY=synthetic-test-key-not-a-credential
/opt/hermes/docker/stage2-hook.sh
python3 - <<'PY'
import os
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
for name in ('unrelated.txt', 'sessions/owned.txt', 'state.db'):
    path = home / name
    path.write_text('preserve content')
    os.chown(path, 12000, 12000)
PY
export HERMES_UID=13000 HERMES_GID=13000
/opt/hermes/docker/stage2-hook.sh
python3 - <<'PY'
import json, os, pwd
from pathlib import Path
home = Path(os.environ['HERMES_HOME'])
paths = ('unrelated.txt', 'sessions/owned.txt', 'state.db', 'sessions', '.')
print(json.dumps({'ownership': {name: [ (home/name).stat().st_uid, (home/name).stat().st_gid ] for name in paths},
                  'contents': (home/'unrelated.txt').read_text(), 'uid': pwd.getpwnam('hermes').pw_uid}))
PY
'''
    script = script.replace("export HERMES_HOME='/srv/custom state'", f"export HERMES_HOME={shlex.quote(home)}")
    result = subprocess.run(
        ['docker', 'run', '--rm', '--pids-limit=-1', '--entrypoint', 'sh', built_image, '-c', script],
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{"ownership"')))
    assert state['uid'] == 13000
    assert state['contents'] == 'preserve content'
    assert state['ownership']['unrelated.txt'] == [12000, 12000]
    for name in ('sessions/owned.txt', 'state.db', 'sessions', '.'):
        assert state['ownership'][name] == [13000, 13000], state
