from pathlib import Path
import pytest


from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.bwrap import BwrapSandbox
from tests.conftest import needs_bwrap, needs_bwrap_userns


@needs_bwrap_userns
def test_bwrap_smoketest_runs_true():
    sb = BwrapSandbox()
    result = sb.smoketest()
    assert result.exit_code == 0
    assert not result.killed


@needs_bwrap_userns
def test_bwrap_runs_command_and_captures_stdout(tmp_path: Path):
    sb = BwrapSandbox()
    req = SandboxRequest(
        argv=["/bin/echo", "hello"],
        limits=Limits(timeout_s=5),
    )
    result = sb.run(req)
    assert result.exit_code == 0
    assert result.stdout.strip() == b"hello"


@needs_bwrap_userns
def test_bwrap_blocks_network():
    sb = BwrapSandbox()
    req = SandboxRequest(
        argv=["/bin/sh", "-c", "getent hosts example.com; echo exit=$?"],
        limits=Limits(timeout_s=5),
    )
    result = sb.run(req)
    # No network namespace + no resolver -> getent fails, our shell line still exits 0.
    assert b"exit=2" in result.stdout or b"exit=1" in result.stdout


@needs_bwrap_userns
def test_bwrap_kills_on_timeout():
    sb = BwrapSandbox()
    req = SandboxRequest(
        argv=["/bin/sh", "-c", "sleep 10"],
        limits=Limits(timeout_s=1),
    )
    result = sb.run(req)
    assert result.killed
    assert result.duration_ms < 5000


@needs_bwrap_userns
def test_bwrap_mounts_input_readonly(tmp_path: Path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "hello.txt").write_text("payload")
    sb = BwrapSandbox()
    req = SandboxRequest(
        argv=["/bin/cat", "/sandbox/in/hello.txt"],
        ro_mounts=[Mount(src, Path("/sandbox/in"), read_only=True)],
        limits=Limits(timeout_s=5),
    )
    result = sb.run(req)
    assert result.exit_code == 0
    assert result.stdout == b"payload"


# -----------------------------------------------------------------------------
# Argv-only tests — these exercise _build_argv without actually spawning bwrap,
# so they run on hosts where user namespaces are blocked.
# -----------------------------------------------------------------------------


@needs_bwrap
def test_bwrap_argv_prefixes_aa_exec_when_available(monkeypatch):
    """When aa-exec is on PATH the inner argv gets an aa-exec prefix."""
    import clippyshot.sandbox.bwrap as bwrap_mod

    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        "/usr/bin/aa-exec" if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    sb = BwrapSandbox()
    assert sb.apparmor_active
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001

    # Everything before "--" is bwrap's own flags; the inner payload starts
    # after the first "--" token.
    dash_idx = argv.index("--")
    inner = argv[dash_idx + 1 :]
    assert inner[:4] == ["/usr/bin/aa-exec", "-p", "clippyshot-soffice", "--"]
    assert inner[4] == "/bin/true"


@needs_bwrap
def test_bwrap_argv_skips_aa_exec_when_not_available(monkeypatch):
    """When aa-exec is not found the argv falls back to the raw payload.

    The WARN log is produced at construction time; here we only assert the
    argv is unchanged so users without apparmor-utils continue to function.
    """
    import clippyshot.sandbox.bwrap as bwrap_mod

    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        None if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    sb = BwrapSandbox()
    assert not sb.apparmor_active
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    dash_idx = argv.index("--")
    inner = argv[dash_idx + 1 :]
    assert inner == ["/bin/true"]


@needs_bwrap
def test_bwrap_apparmor_profile_is_configurable(monkeypatch):
    import clippyshot.sandbox.bwrap as bwrap_mod

    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        "/usr/bin/aa-exec" if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    sb = BwrapSandbox(apparmor_profile="custom-profile")
    assert sb.apparmor_profile == "custom-profile"
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    dash_idx = argv.index("--")
    inner = argv[dash_idx + 1 :]
    assert inner[:4] == ["/usr/bin/aa-exec", "-p", "custom-profile", "--"]


@needs_bwrap
def test_bwrap_apply_rlimits_includes_nofile():
    """The preexec function returned by _apply_rlimits should set RLIMIT_NOFILE."""
    import inspect

    from clippyshot.sandbox.bwrap import _apply_rlimits

    src = inspect.getsource(_apply_rlimits)
    assert "RLIMIT_NOFILE" in src


@needs_bwrap
def test_bwrap_without_a_bpf_program_refuses_to_run(monkeypatch):
    """No BPF program (a non-x86_64 host) -> seccomp_active=False, a "seccomp_missing" reason,
    and run()/spawn() REFUSE rather than run unfiltered -- with or without
    CLIPPYSHOT_WARN_ON_INSECURE, which gVisor tiers set routinely."""
    import clippyshot.sandbox.bwrap as bwrap_mod
    import clippyshot.sandbox.seccomp_denylist as denylist_mod
    from clippyshot.sandbox.seccomp_check import SeccompNotEnforced

    monkeypatch.setattr(denylist_mod, "build_bpf_bytes", lambda: None)
    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        "/usr/bin/aa-exec" if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    monkeypatch.setenv("CLIPPYSHOT_WARN_ON_INSECURE", "1")
    sb = BwrapSandbox()
    assert sb.seccomp_active is False
    assert sb.seccomp_source == "none"
    assert "seccomp_missing" in sb.insecurity_reasons
    assert sb.secure is False
    req = SandboxRequest(argv=["/bin/true"], attach_apparmor=False)
    with pytest.raises(SeccompNotEnforced):
        sb.run(req)
    with pytest.raises(SeccompNotEnforced):
        sb.spawn(req)


@needs_bwrap
def test_bwrap_seccomp_active_when_bpf_builds(monkeypatch):
    """With a BPF program built there is NO "seccomp_missing" reason, but seccomp_active stays
    False until a probe has proven the filter (the first run); then it is
    "clippyshot-bwrap"."""
    import clippyshot.sandbox.bwrap as bwrap_mod
    import clippyshot.sandbox.seccomp_denylist as denylist_mod

    monkeypatch.setattr(denylist_mod, "build_bpf_bytes", lambda: b"\x00" * 16)
    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        "/usr/bin/aa-exec" if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    from clippyshot.sandbox.seccomp_check import SeccompVerdict

    sb = BwrapSandbox()
    assert "seccomp_missing" not in sb.insecurity_reasons
    assert sb.seccomp_active is False and sb.seccomp_source == "none"   # not proven yet
    monkeypatch.setattr(sb, "_seccomp_verdict", SeccompVerdict(True, "test"))
    assert sb.seccomp_active is True
    assert sb.seccomp_source == "clippyshot-bwrap"


@needs_bwrap
def test_bwrap_build_argv_appends_seccomp_fd_before_separator(monkeypatch):
    """_build_argv(seccomp_fd=N) inserts `--seccomp N` in the bwrap options, before the `--`."""
    import clippyshot.sandbox.bwrap as bwrap_mod

    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: (
        "/usr/bin/aa-exec" if name == "aa-exec" else "/usr/bin/bwrap"
    ))
    sb = BwrapSandbox()
    argv = sb._build_argv(SandboxRequest(argv=["/bin/true"]), seccomp_fd=7)
    assert "--seccomp" in argv and argv[argv.index("--seccomp") + 1] == "7"
    assert argv.index("--seccomp") < argv.index("--")   # in options, before the command sep
    # and omitted when no fd is passed
    assert "--seccomp" not in sb._build_argv(SandboxRequest(argv=["/bin/true"]))
