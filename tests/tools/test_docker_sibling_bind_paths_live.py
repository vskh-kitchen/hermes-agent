"""Opt-in Docker-compatible sibling-bind integration with synthetic fixture data."""
import hashlib
import json
import os
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.platforms("linux")


@pytest.mark.skipif(os.getenv("HERMES_RUN_E2E") != "1", reason="opt-in daemon integration")
def test_live_sibling_readonly_bind(monkeypatch, tmp_path):
    suffix = uuid.uuid4().hex
    name = "hermes-bind-test-" + suffix
    # The fixture owns this daemon-host directory, never a user's Hermes home.
    host = "/tmp/" + name
    image = os.getenv("HERMES_DOCKER_BIND_TEST_IMAGE", "python:3.12-bookworm")
    def cli(*args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        cid = cli("run", "-d", "--name", name, "-v", host + ":/gateway", image, "sleep", "infinity")
        cli("exec", cid, "python", "-c", "from pathlib import Path; p=Path('/gateway/cache/documents'); p.mkdir(parents=True); (p/'fixture.txt').write_bytes(b'synthetic attachment')")
        from tools.environments import docker_bind_paths as bp
        # Read the fixture gateway's kernel namespace, not this test runner's.
        # Only the transport runs outside it; discovery/inspect/exec remain real.
        info = tmp_path / "mountinfo"
        info.write_text(cli("exec", cid, "python", "-c", "print(open('/proc/self/mountinfo').read())"))
        group = tmp_path / "cgroup"
        group.write_text(cli("exec", cid, "python", "-c", "print(open('/proc/self/cgroup').read())"))
        identity = json.loads(cli("exec", cid, "python", "-I", "-S", "-c", bp._NAMESPACE_PROBE))
        monkeypatch.setattr(bp, "_namespace_identity", lambda: identity)
        monkeypatch.setattr(bp.sys, "executable", "/usr/local/bin/python")
        def capture(argv, **kw):
            return subprocess.run(argv, check=False, capture_output=True, text=True, **kw)
        sources = bp.discover_bind_sources(capture, "docker", str(info), str(group))
        assert sources.mounts is not None, "live creator discovery fell back to legacy paths"
        source = sources.source("/gateway/cache/documents", readonly=True)
        output = cli("run", "--rm", "-v", source + ":/documents:ro", image, "python", "-c",
                     "import hashlib; from pathlib import Path; p=Path('/documents/fixture.txt'); print(hashlib.sha256(p.read_bytes()).hexdigest());\ntry: p.write_text('overwrite')\nexcept OSError: print('readonly')\nelse: raise AssertionError('bind was writable')")
        assert output.splitlines() == [hashlib.sha256(b"synthetic attachment").hexdigest(), "readonly"]
    finally:
        # Remove only fixture files via its already-bound namespace before teardown.
        subprocess.run(["docker", "exec", name, "python", "-c", "import shutil; shutil.rmtree('/gateway/cache', ignore_errors=True)"], check=False, capture_output=True, timeout=15)
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=15)
