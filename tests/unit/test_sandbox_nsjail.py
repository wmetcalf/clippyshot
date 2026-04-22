from pathlib import Path

import pytest

from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.nsjail import NsjailSandbox
from tests.conftest import needs_nsjail, needs_nsjail_userns


@needs_nsjail_userns
def test_nsjail_smoketest_runs_true():
    sb = NsjailSandbox()
    result = sb.smoketest()
    assert result.exit_code == 0


@needs_nsjail_userns
def test_nsjail_runs_command_and_captures_stdout():
    sb = NsjailSandbox()
    req = SandboxRequest(argv=["/bin/echo", "hello"], limits=Limits(timeout_s=5))
    result = sb.run(req)
    assert result.exit_code == 0
    assert result.stdout.strip() == b"hello"


@needs_nsjail_userns
def test_nsjail_kills_on_timeout():
    sb = NsjailSandbox()
    req = SandboxRequest(argv=["/bin/sh", "-c", "sleep 10"], limits=Limits(timeout_s=1))
    result = sb.run(req)
    assert result.killed


@needs_nsjail_userns
def test_nsjail_mounts_input_readonly(tmp_path: Path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "hello.txt").write_text("payload")
    sb = NsjailSandbox()
    req = SandboxRequest(
        argv=["/bin/cat", "/sandbox/in/hello.txt"],
        ro_mounts=[Mount(src, Path("/sandbox/in"), read_only=True)],
        limits=Limits(timeout_s=5),
    )
    result = sb.run(req)
    assert result.exit_code == 0
    assert result.stdout == b"payload"


# -----------------------------------------------------------------------------
# Argv-only tests (no userns needed — these exercise _build_argv in isolation).
# They pin the seccomp + AppArmor wire-up so the defense layers can't silently
# regress during a refactor.
# -----------------------------------------------------------------------------


@needs_nsjail
def test_nsjail_argv_includes_seccomp_policy():
    """The nsjail argv must include --seccomp_policy with the deployed file."""
    sb = NsjailSandbox()
    # Seccomp policy is resolved at construction time; verify it found the file.
    assert sb.seccomp_active, (
        "seccomp policy was not found; expected "
        "deploy/seccomp/clippyshot.seccomp.policy relative to repo root"
    )
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    assert "--seccomp_policy" in argv
    idx = argv.index("--seccomp_policy")
    assert argv[idx + 1].endswith("clippyshot.seccomp.policy"), argv[idx + 1]


@needs_nsjail
def test_nsjail_argv_includes_apparmor_profile():
    sb = NsjailSandbox()
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    if "--proc_apparmor" in argv:
        idx = argv.index("--proc_apparmor")
        assert argv[idx + 1] == "clippyshot-soffice"


@needs_nsjail
def test_nsjail_apparmor_profile_is_configurable():
    sb = NsjailSandbox(apparmor_profile="custom-profile")
    assert sb.apparmor_profile == "custom-profile"
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    if "--proc_apparmor" in argv:
        idx = argv.index("--proc_apparmor")
        assert argv[idx + 1] == "custom-profile"


@needs_nsjail
def test_nsjail_warns_and_skips_when_seccomp_policy_missing(tmp_path, caplog):
    """If the policy file path is None the backend must not emit --seccomp_policy."""
    bogus = tmp_path / "does-not-exist.policy"
    sb = NsjailSandbox(seccomp_policy=bogus)
    # The constructor logs a WARN; the argv builder must omit --seccomp_policy.
    # We rely on the fact that seccomp_policy=bogus is a missing file and the
    # constructor accepts the path as-is (no existence check for the explicit
    # override — that's an intentional injection point for tests).
    sb._seccomp_policy = None  # noqa: SLF001
    req = SandboxRequest(argv=["/bin/true"], limits=Limits())
    argv = sb._build_argv(req)  # noqa: SLF001
    assert "--seccomp_policy" not in argv
    # --proc_apparmor should still be present — AppArmor is independent.
    if sb._proc_apparmor_supported:  # noqa: SLF001
        assert "--proc_apparmor" in argv


@needs_nsjail
def test_nsjail_skips_proc_apparmor_when_binary_does_not_support_it():
    sb = NsjailSandbox()
    sb._proc_apparmor_supported = False  # noqa: SLF001

    argv = sb._build_argv(SandboxRequest(argv=["/bin/true"], limits=Limits()))  # noqa: SLF001

    assert "--proc_apparmor" not in argv
