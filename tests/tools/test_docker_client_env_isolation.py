"""Container configuration must not reconfigure the outer Docker/Podman client."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.environments import docker


@pytest.fixture
def client_probe(tmp_path, monkeypatch):
    """A real child process records the client env and consumes Docker's env flags.

    This is a CLI-boundary probe, not a Docker daemon or container integration test.
    """
    script = tmp_path / "client.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "values = {}\n"
        "files = []\n"
        "for i, arg in enumerate(args[:-1]):\n"
        "    if arg == '--env-file':\n"
        "        p = pathlib.Path(args[i + 1])\n"
        "        files.append([str(p), p.stat().st_mode & 0o777])\n"
        "        for line in p.read_text(encoding='utf-8').splitlines():\n"
        "            k, v = line.split('=', 1)\n"
        "            values[k] = v\n"
        "for i, arg in enumerate(args[:-1]):\n"
        "    if arg == '-e':\n"
        "        k = args[i + 1]\n"
        "        assert '=' not in k\n"
        "        if k in os.environ: values[k] = os.environ[k]\n"
        "pathlib.Path(os.environ['PROBE_REPORT']).write_text(json.dumps({\n"
        "    'argv': args, 'client': dict(os.environ), 'values': values, 'files': files,\n"
        "    'stdin': sys.stdin.read() if '-i' in args else None}))\n"
        "print('probe-container')\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    monkeypatch.setenv("PROBE_REPORT", str(report))
    real_run = subprocess.run
    real_popen = docker._popen_bash
    # Replace only the executable; subprocess and filesystem IO remain real.
    monkeypatch.setattr(docker, "run_capture", lambda cmd, **kw: real_run(
        [sys.executable, str(script), *cmd[1:]], capture_output=True,
        text=True, stdin=subprocess.DEVNULL, **kw))
    monkeypatch.setattr(docker, "_popen_bash", lambda cmd, data=None, **kw: real_popen(
        [sys.executable, str(script), *cmd[1:]], data, **kw))
    monkeypatch.setattr(docker, "_ensure_docker_available", lambda: None)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker-probe")
    monkeypatch.setattr(docker, "_image_uses_init_entrypoint", lambda *a: False)
    monkeypatch.setattr(docker, "_readonly_skill_mount_args", lambda: [])
    monkeypatch.setattr(docker.DockerEnvironment, "_resource_args", lambda *a: [])
    monkeypatch.setattr(docker.DockerEnvironment, "_mount_args", lambda *a: ([], []))
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(docker.DockerEnvironment, "cleanup", lambda self: None)
    monkeypatch.setattr(docker.DockerEnvironment, "_find_reusable_container", lambda *a: None)
    monkeypatch.setattr(docker.DockerEnvironment, "_resolve_passthrough_env", lambda self: ({}, set()))
    return lambda: json.loads(report.read_text())


@pytest.mark.parametrize("parent_present", [False, True])
def test_container_values_do_not_reconfigure_client(client_probe, monkeypatch, caplog, parent_present):
    child = {
        "DOCKER_HOST": "unix:///inner/podman.sock",
        "DOCKER_CONTEXT": "inner-context",
        "DOCKER_TLS_VERIFY": "",
        "DOCKER_CERT_PATH": "/inner/cert path",
        "CONTAINER_HOST": "unix:///inner/other.sock",
        "CONTAINERS_CONF": "/inner/containers.conf",
        "REGISTRY_AUTH_FILE": "/inner/registry-auth.json",
        "STORAGE_DRIVER": "inner-storage",
        "STORAGE_OPTS": "inner-options",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/inner/bus",
        "HOME": "/inner/home",
        "PATH": "/inner/bin",
        "SSH_AUTH_SOCK": "/inner/ssh.sock",
        "XDG_CONFIG_HOME": "/inner/config",
        "HTTP_PROXY": "http://inner:4321",
        "https_proxy": "http://inner:4322",
        "SSL_CERT_FILE": "/inner/ca.pem",
        "SERVICE_SECRET": " first='quoted'\r\nsecond=$literal\\tail\n",
        "EMPTY_SECRET": "",
        "PLAIN_SECRET": " spaced=é # literal ",
    }
    controls = set(child) - {"SERVICE_SECRET", "EMPTY_SECRET", "PLAIN_SECRET"}
    for key in controls:
        if parent_present or key in {"HOME", "PATH"}:
            monkeypatch.setenv(key, os.environ.get(key, "outer-" + key))
        else:
            monkeypatch.delenv(key, raising=False)
    parent = dict(os.environ)
    egress = {"HTTPS_PROXY": "http://egress-only:8899", "REQUESTS_CA_BUNDLE": "/egress/ca.pem"}
    monkeypatch.setattr(docker, "_egress_proxy_args_for_docker", lambda: ([], egress, []))
    # No collision in this probe: egress transport is tested alongside operator config.
    monkeypatch.setattr(docker, "_egress_enforce_on_docker", lambda: False)
    caplog.set_level("DEBUG", logger=docker.__name__)
    env = docker.DockerEnvironment(image="probe-image", env=child, persist_across_processes=False)

    def check(expected, stdin=None):
        result = client_probe()
        for key in controls | set(egress):
            assert result["client"].get(key) == parent.get(key), key
        assert result["values"] == expected
        assert result["stdin"] == stdin
        for value in expected.values():
            if value:
                assert value not in " ".join(result["argv"])
                assert value not in caplog.text
        for name, mode in result["files"]:
            if os.name != "nt":
                assert mode == 0o600
        return result["files"]

    run_files = check({**child, **egress})
    assert run_files
    assert all(not Path(name).exists() for name, _ in run_files)
    assert env._recreate_container()
    recovery_files = check({**child, **egress})
    assert all(not Path(name).exists() for name, _ in recovery_files)
    assert {p for p, _ in run_files}.isdisjoint(p for p, _ in recovery_files)

    for login in (True, False):
        monkeypatch.setattr(env, "_resolve_passthrough_env", lambda: (child, set()))
        env._init_env_args = env._build_init_env_args()
        proc = env._run_bash("printf probe", login=login, stdin_data="user input\n")
        assert proc.communicate(timeout=10)[0].strip() == "probe-container"
        assert proc.returncode == 0
        files = check(child, stdin="user input\n")
        # Cleanup is asynchronous because Popen must return before the CLI reads the file.
        cleanup = getattr(proc, "_hermes_env_cleanup_thread", None)
        if cleanup:
            cleanup.join(timeout=10)
            assert not cleanup.is_alive()
        assert all(not Path(name).exists() for name, _ in files)
    assert dict(os.environ) == parent

    failed_paths = []

    def fail_spawn(cmd, *args, **kwargs):
        path = Path(cmd[cmd.index("--env-file") + 1])
        assert path.exists()
        failed_paths.append(path)
        raise OSError("probe spawn failure")

    monkeypatch.setattr(docker, "_popen_bash", fail_spawn)
    with pytest.raises(OSError, match="probe spawn failure"):
        env._run_bash("printf probe")
    monkeypatch.setattr(docker, "run_capture", fail_spawn)
    with pytest.raises(OSError, match="probe spawn failure"):
        env._docker_run(env.cwd)
    assert failed_paths and all(not path.exists() for path in failed_paths)


@pytest.mark.parametrize("key", ["DOCKER_HOST", "HOME", "https_proxy", "SSL_CERT_FILE"])
@pytest.mark.parametrize("value", [
    pytest.param("hidden\nvalue", id="LF"),
    pytest.param("hidden\rvalue", id="CR"),
    pytest.param("hidden\x00value", id="NUL"),
    pytest.param("hidden\udcffvalue", id="non-UTF8"),
    pytest.param("hidden" * 12000, id="oversize"),
])
@pytest.mark.parametrize("phase", ["run", "login", "exec", "recovery"])
def test_unrepresentable_control_values_fail_without_disclosure(client_probe, monkeypatch, caplog, key, value, phase):
    monkeypatch.setattr(docker, "_egress_proxy_args_for_docker", lambda: ([], {}, []))
    if phase != "run":
        env = docker.DockerEnvironment(image="probe-image", persist_across_processes=False)
        env._run_env_values = {key: value}
        env._env = {key: value}
        monkeypatch.setattr(env, "_resolve_passthrough_env", lambda: ({key: value}, set()))
        env._init_env_args = env._build_init_env_args()
    with pytest.raises(ValueError) as exc:
        if phase == "run":
            docker.DockerEnvironment(image="probe-image", env={key: value}, persist_across_processes=False)
        elif phase == "recovery":
            env._recreate_container()
        else:
            env._run_bash("printf probe", login=phase == "login")
    assert key in str(exc.value)
    assert "hidden" not in str(exc.value) + caplog.text
