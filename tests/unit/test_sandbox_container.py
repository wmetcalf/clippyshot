"""Unit tests for ContainerSandbox."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from clippyshot.errors import SandboxUnavailable
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.container import ContainerSandbox, _inside_container, _translate_path


# ---------------------------------------------------------------------------
# _translate_path
# ---------------------------------------------------------------------------

def test_translate_path_rewrites_prefix():
    path_map = {"/sandbox/in": "/host/input"}
    result = _translate_path("/sandbox/in/hello.txt", path_map)
    assert result == "/host/input/hello.txt"


def test_translate_path_exact_match():
    path_map = {"/sandbox/in": "/host/input"}
    result = _translate_path("/sandbox/in", path_map)
    assert result == "/host/input"


def test_translate_path_no_match_unchanged():
    path_map = {"/sandbox/in": "/host/input"}
    result = _translate_path("/other/path/file.txt", path_map)
    assert result == "/other/path/file.txt"


def test_translate_path_longest_prefix_wins():
    """'/sandbox/input' must win over '/sandbox/in' for paths under it."""
    path_map = {
        "/sandbox/in": "/host/short",
        "/sandbox/input": "/host/long",
    }
    result = _translate_path("/sandbox/input/doc.docx", path_map)
    assert result == "/host/long/doc.docx"


def test_translate_path_shorter_prefix_wins_when_longer_doesnt_match():
    path_map = {
        "/sandbox/in": "/host/short",
        "/sandbox/input": "/host/long",
    }
    result = _translate_path("/sandbox/in/file.txt", path_map)
    assert result == "/host/short/file.txt"


def test_translate_path_embedded_in_url():
    """Embedded occurrence: '-env:UserInstallation=file:///sandbox/profile'."""
    path_map = {"/sandbox/profile": "/tmp/lo-profile-abc"}
    result = _translate_path(
        "-env:UserInstallation=file:///sandbox/profile/",
        path_map,
    )
    assert result == "-env:UserInstallation=file:///tmp/lo-profile-abc/"


def test_translate_path_embedded_exact_suffix():
    path_map = {"/sandbox/profile": "/tmp/lo-profile-abc"}
    result = _translate_path("file:///sandbox/profile", path_map)
    assert result == "file:///tmp/lo-profile-abc"


# ---------------------------------------------------------------------------
# ContainerSandbox.__init__ guards
# ---------------------------------------------------------------------------

def test_refuses_when_not_in_container(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    with pytest.raises(SandboxUnavailable, match="not running inside a container"):
        ContainerSandbox()


def test_refuses_when_running_as_root(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(SandboxUnavailable, match="root"):
        ContainerSandbox()


# ---------------------------------------------------------------------------
# ContainerSandbox.smoketest
# ---------------------------------------------------------------------------

def test_smoketest_returns_exit_0(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    sb = ContainerSandbox()
    result = sb.smoketest()
    assert result.exit_code == 0
    assert not result.killed


# ---------------------------------------------------------------------------
# ContainerSandbox.run — path translation + real execution
# ---------------------------------------------------------------------------

def test_run_reads_file_via_mount(monkeypatch, tmp_path):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    sb = ContainerSandbox()

    # Create a real file on the host
    host_file = tmp_path / "hello.txt"
    host_file.write_bytes(b"hello from host\n")

    mount = Mount(
        host_path=tmp_path,
        sandbox_path=Path("/sandbox/in"),
        read_only=True,
    )
    request = SandboxRequest(
        argv=["/bin/cat", "/sandbox/in/hello.txt"],
        ro_mounts=[mount],
    )
    result = sb.run(request)
    assert result.exit_code == 0
    assert result.stdout == b"hello from host\n"


def test_run_nonexistent_binary_raises(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    sb = ContainerSandbox()

    from clippyshot.errors import SandboxError
    request = SandboxRequest(argv=["/nonexistent/binary"])
    with pytest.raises(SandboxError, match="failed to start"):
        sb.run(request)


def test_container_marks_runtime_insecure_when_hardening_missing(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "clippyshot.sandbox.container._runtime_hardening_reasons",
        lambda: ["rootfs_not_read_only", "seccomp_not_enforced"],
    )

    sb = ContainerSandbox()

    assert sb.secure is False
    assert sb.insecurity_reasons == [
        "rootfs_not_read_only",
        "seccomp_not_enforced",
    ]


def test_container_reports_secure_when_runtime_hardening_present(monkeypatch):
    monkeypatch.setattr("clippyshot.sandbox.container._inside_container", lambda: True)
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    monkeypatch.setattr(
        "clippyshot.sandbox.container._runtime_hardening_reasons",
        lambda: [],
    )

    sb = ContainerSandbox()

    assert sb.secure is True
    assert sb.insecurity_reasons == []
