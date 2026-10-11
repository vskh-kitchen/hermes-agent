"""Resolve automatic bind sources across a container/daemon namespace boundary.

Inspect records require both a full kernel container ID and independent
kernel-boot/hostname-inode corroboration. Host path spelling is not identity.
Never apply this to operator-supplied Docker volume specifications.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import subprocess
from pathlib import Path

logger = logging.getLogger("tools.environments.docker")
_CONTAINER_ID = re.compile(r"(?:/containers/|/docker/|docker-|libpod-)([0-9a-f]{64})(?:/|\.scope|$)")
_HOSTNAME_ID = re.compile(r"(?:^|/)containers/([0-9a-f]{64})/hostname$")
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# Run with the creator's interpreter, without site hooks or Python environment
# overrides. This only reads kernel metadata; it neither writes files nor starts
# a sibling container. An unavailable interpreter/exec permission fails discovery.
_NAMESPACE_PROBE = (
    "import json, os; from pathlib import Path; "
    "s = os.stat('/etc/hostname'); "
    "print(json.dumps({'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), "
    "'device': s.st_dev, 'inode': s.st_ino}))"
)


def _decode_mountinfo(value: str) -> str:
    # Decode once: a literal backslash followed by 040 must not turn into a space.
    return _MOUNTINFO_ESCAPE.sub(lambda m: chr(int(m[1], 8)), value)


def container_identity(mountinfo: str, cgroup: str) -> tuple[str, str | None]:
    """Full kernel ID, and filesystem-relative hostname root when available.

    Neither HOSTNAME nor the mount root is a daemon-host path. Conflicting
    hostname/cgroup identities are rejected rather than picking one witness.
    """
    identities = {m[1] for m in _CONTAINER_ID.finditer(cgroup)}
    hostname_root = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and _decode_mountinfo(fields[4]) == "/etc/hostname":
            root = _decode_mountinfo(fields[3])
            match = _HOSTNAME_ID.search(root)
            if match:
                identities.add(match[1])
                hostname_root = root
    if len(identities) > 1:
        raise ValueError("conflicting kernel container identities")
    return (next(iter(identities)), hostname_root) if identities else ("", None)


def _namespace_identity() -> dict:
    """Identify the hostname file on this kernel boot, independent of host paths.

    Device/inode are only comparable on the same kernel. The boot UUID rejects
    accidental remote matches, including copied IDs, paths and filesystem inodes.
    This assumes trusted Docker/kernel metadata, not a malicious daemon or a
    cloned/spoofed boot UUID. No inspect-only global identity proof is possible.
    """
    st = os.stat("/etc/hostname")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not _BOOT_ID.fullmatch(boot_id):
        raise ValueError("kernel boot identity unavailable")
    return {"boot_id": boot_id, "device": st.st_dev, "inode": st.st_ino}


class BindPathError(RuntimeError):
    """A discovered creator source cannot safely be bound into a sibling."""


class BindSources:
    def __init__(self, mounts: list[dict] | None = None):
        self.mounts = mounts

    def source(self, path: str, *, readonly: bool = False) -> str:
        if self.mounts is None or re.match(r"^[A-Za-z]:[\\/]", path):
            return path
        real = os.path.realpath(path)
        covering = [m for m in self.mounts if Path(real).is_relative_to(m["Destination"])]
        if not covering:
            raise BindPathError(f"Docker: automatic bind source {path!r} is in the creator container layer; "
                               "place it on a gateway bind mount (including TMPDIR for staged skills).")
        mount = max(covering, key=lambda m: len(m["Destination"]))
        if mount.get("Type") != "bind":
            raise BindPathError(f"Docker: automatic bind source {path!r} is on a non-bind mount; "
                               "named volumes/tmpfs cannot safely be translated to host binds.")
        if not readonly and mount.get("RW") is not True:
            raise BindPathError(f"Docker: refusing writable sandbox bind from read-only gateway mount: {path!r}")
        # Binding a parent's daemon source would bypass child mounts in the creator,
        # possibly exposing hidden data. Refuse rather than silently change the tree.
        if any(m["Destination"] != real and Path(m["Destination"]).is_relative_to(real)
               for m in self.mounts):
            raise BindPathError(f"Docker: automatic directory bind {path!r} contains nested gateway mounts; "
                               "mount the required subtree separately.")
        relative = os.path.relpath(real, mount["Destination"])
        return os.path.normpath(os.path.join(mount["Source"], relative))

    def fingerprint(self) -> str:
        if self.mounts is None:
            return ""
        return json.dumps(sorted(self.mounts, key=lambda m: m["Destination"]), sort_keys=True)


def discover_bind_sources(run_capture, executable: str, mountinfo_path: str, cgroup_path: str) -> BindSources:
    if not sys.platform.startswith("linux"):
        return BindSources()
    def read(path):
        try:
            return Path(path).read_text()
        except OSError:
            return ""
    # Select only necessary fields: inspect's full record contains credential env.
    template = '{"Id":{{json .Id}},"HostnamePath":{{json .HostnamePath}},"Mounts":{{json .Mounts}}}'
    try:
        cid, hostname_root = container_identity(read(mountinfo_path), read(cgroup_path))
        if not cid:
            return BindSources()
        local_identity = _namespace_identity()
        result = run_capture([executable, "inspect", "--format", template, cid], timeout=5)
        if result.returncode != 0:
            raise ValueError("creator container is not inspectable on the selected daemon")
        record = json.loads(result.stdout)
        if not isinstance(record, dict):
            raise ValueError("invalid inspect record")
        if record.get("Id") != cid:
            raise ValueError("inspect identity does not match the creator namespace")
        if hostname_root:
            # Structural consistency only, NOT path corroboration: mountinfo root
            # is relative to its filesystem, whose host mount point is unknown.
            hostname_path = record.get("HostnamePath")
            match = _HOSTNAME_ID.search(hostname_path) if isinstance(hostname_path, str) else None
            if not match or match[1] != cid or not hostname_path.startswith("/"):
                raise ValueError("invalid inspect hostname identity")
        probe = run_capture([executable, "exec", cid, sys.executable, "-I", "-S", "-c",
                             _NAMESPACE_PROBE], timeout=5)
        if probe.returncode != 0 or json.loads(probe.stdout) != local_identity:
            raise ValueError("daemon creator does not share the gateway kernel/hostname inode")
        # A recreated/replaced hostname file during discovery must not be trusted.
        if _namespace_identity() != local_identity:
            raise ValueError("gateway identity changed during discovery")
        mounts = record["Mounts"]
        if not isinstance(mounts, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("Destination"), str)
            or not m["Destination"].startswith("/")
            or (m.get("Type") == "bind" and (not isinstance(m.get("Source"), str)
                                              or not m["Source"].startswith("/"))) for m in mounts):
            raise ValueError("invalid inspect mount table")
        return BindSources([{k: m.get(k) for k in ("Type", "Source", "Destination", "RW")} for m in mounts])
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        logger.warning("Docker: cannot discover creator bind paths (%s); using legacy source paths. "
                       "A remote daemon may not share this filesystem.", exc)
        return BindSources()
