"""Automatic binds use the creator's namespace, explicit volumes use the daemon's."""
import json
import subprocess
from pathlib import Path

import pytest

from tools.environments import docker


pytestmark = pytest.mark.platforms("linux")

CID = "a" * 64


def _gateway(monkeypatch, tmp_path, mounts):
    info = tmp_path / "mountinfo"
    info.write_text(f"1 0 8:1 /var/lib/docker/containers/{CID}/hostname /etc/hostname rw - ext4 /dev/sda rw\n")
    monkeypatch.setattr(docker, "_gateway_mountinfo_path", str(info), raising=False)
    monkeypatch.setattr(docker, "_gateway_cgroup_path", str(tmp_path / "absent"), raising=False)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    record = {"Id": CID, "HostnamePath": f"/var/lib/docker/containers/{CID}/hostname", "Mounts": mounts}
    import os
    stat = os.stat("/etc/hostname")
    identity = {"boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "device": stat.st_dev, "inode": stat.st_ino}
    def run(argv, **kw):
        output = identity if argv[1] == "exec" else record
        return subprocess.CompletedProcess(argv, 0, json.dumps(output), "")
    monkeypatch.setattr(docker, "run_capture", run)
    return record


def _bind(source, destination, rw=True, kind="bind"):
    return {"Type": kind, "Source": str(source), "Destination": str(destination), "RW": rw}


def test_readonly_mounts_translate_without_changing_media_paths(monkeypatch, tmp_path):
    from tools import credential_files as cf
    home = tmp_path / "gateway"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    docs = home / "cache" / "documents"
    docs.mkdir(parents=True)
    (docs / "example.txt").write_text("attachment")
    _gateway(monkeypatch, tmp_path, [_bind("/daemon/state", home)])
    args = docker._readonly_skill_mount_args()
    assert any("/daemon/state/cache/documents" in arg for arg in args)
    assert cf.map_cache_path_to_container(str(docs / "example.txt")) == "/root/.hermes/cache/documents/example.txt"
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert cf.from_agent_visible_cache_path("/root/.hermes/cache/documents/example.txt") == str(docs / "example.txt")
    assert all(arg.endswith(":ro") for arg in args if arg != "-v")
    assert (docs / "example.txt").read_text() == "attachment"


def test_persistent_and_cwd_binds_translate_but_explicit_volumes_do_not(monkeypatch, tmp_path):
    home = tmp_path / "gateway"
    home.mkdir()
    project = home / "project"
    project.mkdir()
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", str(home / "sandboxes"))
    _gateway(monkeypatch, tmp_path, [_bind("/daemon/state", home)])
    env = docker.DockerEnvironment.__new__(docker.DockerEnvironment)
    env._persistent = True
    volumes, writable = env._mount_args([f"{home}:/explicit:ro"], str(project), True, "session")
    assert f"{home}:/explicit:ro" in volumes
    assert any("/daemon/state/project" in arg for arg in volumes)
    assert any("/daemon/state/sandboxes/docker/session/home" in arg for arg in writable)
    assert env.host_cwd == str(project)
    assert env._home_dir == str(home / "sandboxes/docker/session/home")
    assert not any("/daemon/state/sandboxes/docker/session/workspace" in arg for arg in writable)
    volumes, _ = env._mount_args(["/daemon/state/project:/custom:ro"], str(project), True, "session")
    assert volumes == ["-v", "/daemon/state/project:/custom:ro"]
    assert env.host_cwd_mount == "/custom"
    _, writable = env._mount_args([], None, False, "session")
    assert "/daemon/state/sandboxes/docker/session/workspace:/workspace" in writable


@pytest.mark.parametrize("kind", ["volume", "tmpfs"])
def test_nested_mount_uses_longest_prefix_and_rejects_non_binds(kind):
    from tools.environments.docker_bind_paths import BindSources
    sources = BindSources([_bind("/host/state", "/gateway"),
                           _bind("/host/cache", "/gateway/cache"),
                           _bind("/host/volume", "/gateway/cache/private", kind=kind)])
    assert sources.source("/gateway/cache/documents", readonly=True) == "/host/cache/documents"
    with pytest.raises(RuntimeError, match="non-bind"):
        sources.source("/gateway/cache/private/token", readonly=True)
    with pytest.raises(RuntimeError, match="nested"):
        sources.source("/gateway/cache", readonly=True)
    with pytest.raises(RuntimeError, match="container layer"):
        sources.source("/gateway-other/file", readonly=True)


def test_readonly_scope_symlinks_and_namespace_fingerprint(tmp_path):
    from tools.environments.docker_bind_paths import BindSources
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    sources = BindSources([_bind("/host/real", real, rw=False)])
    assert sources.source(str(alias / "file"), readonly=True) == "/host/real/file"
    with pytest.raises(RuntimeError, match="read-only"):
        sources.source(str(alias / "file"))
    other = BindSources([_bind("/host/other", real, rw=False)])
    kw = dict(image="image", mount_args=["-v", "/tmp/staged:/skills:ro"], hermes_home=str(real))
    assert docker._reuse_environment_fingerprint(**kw, bind_namespace=sources.fingerprint()) != docker._reuse_environment_fingerprint(**kw, bind_namespace=other.fingerprint())


@pytest.mark.parametrize("failure", ["timeout", "not-found", "wrong-id", "wrong-hostname", "bad-json", "bad-mounts"])
def test_discovery_failure_does_not_guess_remote_host_paths(monkeypatch, tmp_path, caplog, failure):
    record = _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    def run(*a, **kw):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(a, 5)
        if failure == "not-found":
            return subprocess.CompletedProcess(a, 1, "", "not found")
        if failure == "wrong-id":
            record["Id"] = "b" * 64
        if failure == "wrong-hostname":
            record["HostnamePath"] = "/different/hostname"
        if failure == "bad-mounts":
            record["Mounts"] = None
        return subprocess.CompletedProcess(a, 0, "{" if failure == "bad-json" else json.dumps(record), "")
    monkeypatch.setattr(docker, "run_capture", run)
    sources = docker._automatic_bind_sources()
    assert sources.source("/gateway/cache", readonly=True) == "/gateway/cache"
    assert "cannot discover" in caplog.text


def test_identity_discovery_and_no_container_no_probe(monkeypatch, tmp_path):
    from tools.environments.docker_bind_paths import container_identity
    assert container_identity("", f"2:cpu:/docker/{CID}") == (CID, None)
    assert container_identity("", "0::/") == ("", None)
    monkeypatch.setattr(docker, "_gateway_mountinfo_path", str(tmp_path / "absent"))
    monkeypatch.setattr(docker, "_gateway_cgroup_path", str(tmp_path / "absent"))
    monkeypatch.setattr(docker, "run_capture", lambda *a, **kw: pytest.fail("unexpected inspect"))
    assert docker._automatic_bind_sources().source("C:/workspace") == "C:/workspace"
    assert docker._automatic_bind_sources().source("/local/path") == "/local/path"


def test_credentials_and_staged_skills_preserve_leaf_readonly_scope(monkeypatch, tmp_path):
    from tools import credential_files as cf
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = home / "service.token"
    token.write_text("synthetic-service-token")
    skills = home / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("skill")
    (skills / "omitted").symlink_to(token)
    scratch = home / "scratch"
    scratch.mkdir()
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    assert cf.register_credential_file("service.token")
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", home)])
    args = docker._readonly_skill_mount_args()
    assert "/host/state/service.token:/root/.hermes/service.token:ro" in args
    assert any("/host/state/scratch/hermes-skills-safe-" in arg and arg.endswith(":/root/.hermes/skills:ro") for arg in args)
    assert "/host/state:/root/.hermes:ro" not in args


@pytest.mark.parametrize("root", [
    f"/containers/{CID}/hostname",
    f"/docker\\040data/containers/{CID}/hostname",
    f"/docker\\011data/containers/{CID}/hostname",
    f"/docker\\134040data/containers/{CID}/hostname",
])
def test_filesystem_relative_and_escaped_hostname_root(monkeypatch, tmp_path, root):
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    Path(docker._gateway_mountinfo_path).write_text(
        f"1 0 8:1 {root} /etc/hostname rw - ext4 /dev/sda rw\n")
    assert docker._automatic_bind_sources().source("/gateway/cache", readonly=True) == "/host/state/cache"


@pytest.mark.parametrize("field", ["boot_id", "device", "inode"])
def test_coincident_remote_id_and_path_are_not_identity_proof(monkeypatch, tmp_path, caplog, field):
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    original = docker.run_capture
    def run(argv, **kw):
        result = original(argv, **kw)
        if argv[1] == "exec":
            identity = json.loads(result.stdout)
            identity[field] = "different" if field == "boot_id" else identity[field] + 1
            return subprocess.CompletedProcess(argv, 0, json.dumps(identity), "")
        return result
    monkeypatch.setattr(docker, "run_capture", run)
    assert docker._automatic_bind_sources().mounts is None
    assert "cannot discover" in caplog.text


@pytest.mark.parametrize("failure", ["denied", "timeout", "bad-json", "missing-fields"])
def test_namespace_probe_failure_preserves_legacy_paths(monkeypatch, tmp_path, caplog, failure):
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    original = docker.run_capture
    def run(argv, **kw):
        if argv[1] != "exec":
            return original(argv, **kw)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(argv, 1 if failure == "denied" else 0,
                                           "{" if failure == "bad-json" else "{}", "")
    monkeypatch.setattr(docker, "run_capture", run)
    assert docker._automatic_bind_sources().mounts is None
    assert "cannot discover" in caplog.text


def test_cgroup_identity_also_requires_inode_corroboration(monkeypatch, tmp_path):
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    Path(docker._gateway_mountinfo_path).write_text("")
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(f"2:cpu:/docker/{CID}")
    monkeypatch.setattr(docker, "_gateway_cgroup_path", str(cgroup))
    assert docker._automatic_bind_sources().source("/gateway/cache", readonly=True) == "/host/state/cache"
    original = docker.run_capture
    def run(argv, **kw):
        if argv[1] == "exec":
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        return original(argv, **kw)
    monkeypatch.setattr(docker, "run_capture", run)
    assert docker._automatic_bind_sources().mounts is None


def test_namespace_probe_reads_actual_kernel_metadata():
    import sys
    from tools.environments.docker_bind_paths import _NAMESPACE_PROBE, _namespace_identity
    result = subprocess.run([sys.executable, "-I", "-S", "-c", _NAMESPACE_PROBE],
                            check=True, capture_output=True, text=True, timeout=5)
    assert json.loads(result.stdout) == _namespace_identity()


def test_conflicting_kernel_ids_reject_inspect(monkeypatch, tmp_path):
    _gateway(monkeypatch, tmp_path, [_bind("/host/state", "/gateway")])
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("2:cpu:/docker/" + "b" * 64)
    monkeypatch.setattr(docker, "_gateway_cgroup_path", str(cgroup))
    monkeypatch.setattr(docker, "run_capture", lambda *a, **kw: pytest.fail("conflicting identity must not be inspected"))
    assert docker._automatic_bind_sources().mounts is None


def test_mountinfo_escapes_are_decoded_once():
    from tools.environments.docker_bind_paths import container_identity
    root = f"/docker\\040data\\011tab\\134040literal/containers/{CID}/hostname"
    expected = f"/docker data\ttab\\040literal/containers/{CID}/hostname"
    assert container_identity(f"1 0 8:1 {root} /etc/hostname rw - ext4 /dev/sda rw", "") == (CID, expected)


def test_proxy_ca_source_is_translated_before_egress_fingerprint(monkeypatch):
    from tools.environments.docker_bind_paths import BindSources
    env = docker.DockerEnvironment.__new__(docker.DockerEnvironment)
    env._bind_sources = BindSources([_bind("/host/state", "/gateway")])
    env._forward_env = []
    env._env = {}
    monkeypatch.setattr(docker, "_egress_proxy_args_for_docker",
                        lambda: (["-v", "/gateway/ca.crt:/etc/ssl/ca.crt:ro"], {}, []))
    label, volumes, _, _, _ = env._egress_and_env_args([])
    assert volumes == ["-v", "/host/state/ca.crt:/etc/ssl/ca.crt:ro"]
    assert label == docker._egress_reuse_fingerprint(volumes, {}, [])
