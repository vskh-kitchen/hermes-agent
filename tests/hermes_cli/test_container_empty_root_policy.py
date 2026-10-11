"""Explicit empty container roots retain the Python consumers' semantics."""
import pytest

from agent.file_safety import get_safe_write_roots, is_write_denied
from hermes_cli.web_server_files import _managed_files_policy


@pytest.mark.parametrize("home", ["/opt/data", "/srv/custom state"])
def test_empty_dashboard_root_selects_existing_policy(monkeypatch, home):
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", "")
    policy = _managed_files_policy(None, create_root=False)
    assert str(policy.default_path) == home
    assert policy.can_change_path == (home != "/opt/data")
    assert (policy.locked_root is not None) == (home == "/opt/data")


def test_empty_write_root_removes_containment_not_denylist(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", "")
    assert get_safe_write_roots() == set()
    assert not is_write_denied(str(tmp_path / "ordinary.txt"))
    assert is_write_denied("/etc/shadow")
