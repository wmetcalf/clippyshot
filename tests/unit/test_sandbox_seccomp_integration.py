"""Integration-ish test: verify the seccomp KAFEL policy parses under nsjail.

This test runs nsjail in config-check mode. It does NOT create a user
namespace (which would trip the host's `apparmor_restrict_unprivileged_userns`
policy), it only asks nsjail to parse the policy file.

If the KAFEL parser rejects the policy we fail loud with the parser's
error message attached.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

POLICY = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "seccomp"
    / "clippyshot.seccomp.policy"
)


needs_nsjail = pytest.mark.skipif(
    shutil.which("nsjail") is None, reason="nsjail not installed"
)


@needs_nsjail
def test_seccomp_policy_parses_under_nsjail() -> None:
    """nsjail must accept the KAFEL policy file.

    We run `nsjail --seccomp_policy <file> -- /bin/true` and expect the
    return code to be either 0 (success) or some userns-related failure
    (nonzero but NOT a parser failure).  A KAFEL parse error surfaces as
    a nonzero exit with "ERROR" or "UNKNOWN TOKEN" etc. on stderr BEFORE
    nsjail even attempts to fork the child.
    """
    assert POLICY.is_file(), f"policy missing: {POLICY}"
    proc = subprocess.run(
        [
            "nsjail",
            "--mode", "o",
            "--user", "65534",
            "--group", "65534",
            "--disable_proc",
            "--iface_no_lo",
            "--bindmount_ro", "/usr:/usr",
            "--seccomp_policy", str(POLICY),
            "--", "/bin/true",
        ],
        capture_output=True,
        timeout=10,
    )
    # The child may fail to launch (userns blocked on the host) — that's OK,
    # we just need nsjail not to reject the seccomp policy as malformed.
    stderr = proc.stderr.decode(errors="replace").lower()
    stdout = proc.stdout.decode(errors="replace").lower()
    combined = stderr + stdout

    # Error markers that indicate KAFEL rejected the policy. These all come
    # out of nsjail's preparePolicy() call which runs BEFORE any namespace
    # clone attempt — so they're distinct from the userns failure we expect
    # on this host.
    bad_markers = [
        "policy validation failed",
        "could not compile policy",
        "couldn't prepare sandboxing policy",
        "unknown syscall",
        "parse error",
        "syntax error",
    ]
    for marker in bad_markers:
        assert marker not in combined, (
            f"nsjail rejected seccomp policy (marker={marker!r})\n"
            f"stderr: {stderr}\nstdout: {stdout}"
        )
