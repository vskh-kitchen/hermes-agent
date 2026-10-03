"""Keep container environment values out of the outer Docker/Podman client's config.

Ordinary values use name-only ``-e`` flags (including multiline secrets). Names
interpreted by the client, its runtime, or its SSH/credential helpers instead use
an owner-only env file. Never put values on argv, even as a fallback.
"""

import os
from pathlib import Path
import tempfile
import threading

from tools.environments.remote_common import client_env_with


_CLIENT_ENV_PREFIXES = (
    "DOCKER_", "CONTAINER_", "CONTAINERS_", "PODMAN_", "BUILDKIT_",
    "XDG_", "SSH_", "SSL_", "LD_", "DYLD_",
)
_CLIENT_ENV_NAMES = frozenset({
    "HOME", "PATH", "PATHEXT", "USER", "LOGNAME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
    "WINDIR", "COMSPEC", "TMPDIR", "TMP", "TEMP",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "REGISTRY_AUTH_FILE", "REGISTRY_AUTH_PREFERENCE", "STORAGE_DRIVER", "STORAGE_OPTS",
    "DBUS_SESSION_BUS_ADDRESS",
    "GODEBUG", "GOMAXPROCS", "GOMEMLIMIT", "GOTRACEBACK", "GOTMPDIR",
})


def is_client_env(name: str) -> bool:
    """Case-insensitive also covers Windows and lower-case Go HTTP proxy names."""
    upper = name.upper()
    return upper.startswith(_CLIENT_ENV_PREFIXES) or upper in _CLIENT_ENV_NAMES


def name_only_env_args(names) -> list[str]:
    return [arg for key in sorted(names) if not is_client_env(key) for arg in ("-e", key)]


class DockerEnvTransport:
    """Per-invocation env file: closed on failure, retained until an exec client exits.

    Docker/Podman's line-oriented env-file format cannot encode CR/LF. Reject
    those only for client-control names; ordinary multiline values still travel
    through the client's environment unchanged. NUL cannot be an OS env value.
    """

    def __init__(self, values: dict[str, str]):
        self._file_values = {}
        ordinary = {}
        for key, value in values.items():
            if "\x00" in value:
                raise ValueError(f"Container environment variable {key} cannot contain NUL")
            if is_client_env(key):
                if "\r" in value or "\n" in value:
                    raise ValueError(
                        f"Container environment variable {key} controls the Docker/Podman client; "
                        "its container value cannot contain CR or LF (env-file limitation)")
                try:
                    line_size = len(f"{key}={value}".encode("utf-8"))
                except UnicodeEncodeError:
                    raise ValueError(
                        f"Container environment variable {key} must be valid UTF-8 for env-file transport"
                    ) from None
                # Both CLIs use Go's bufio.Scanner (64 KiB including newline).
                if line_size >= 65536:
                    raise ValueError(
                        f"Container environment variable {key} exceeds the env-file line limit (64 KiB)")
                self._file_values[key] = value
            else:
                ordinary[key] = value
        self.client_env = client_env_with(ordinary)
        self.args: list[str] = []
        self._path: Path | None = None
        self._process = None

    def __enter__(self):
        if self._file_values:
            # mkstemp creates mode 0600 atomically; close before the CLI opens it
            # (including on Windows). The file is never mounted in the container.
            fd, path = tempfile.mkstemp(prefix="hermes-docker-env-", suffix=".env")
            self._path = Path(path)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    for key, value in sorted(self._file_values.items()):
                        stream.write(f"{key}={value}\n")
                self.args = ["--env-file", path]
            except BaseException:
                self._cleanup()
                raise
        return self

    def keep_until_exit(self, process):
        self._process = process

    def _cleanup(self):
        if self._path is not None:
            self._path.unlink(missing_ok=True)

    def __exit__(self, exc_type, exc, tb):
        if self._path is None:
            return
        if self._process is None:
            self._cleanup()
            return

        # Popen returning does not mean the CLI has read --env-file yet. A
        # waiter owns cleanup without consuming stdout or interfering with stdin.
        def cleanup_after_exit():
            try:
                self._process.wait()
            finally:
                self._cleanup()

        thread = threading.Thread(target=cleanup_after_exit, daemon=True, name="docker-env-cleanup")
        self._process._hermes_env_cleanup_thread = thread
        thread.start()
