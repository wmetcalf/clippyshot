from pathlib import Path

import pytest

from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.detect import select_sandbox
from tests.conftest import needs_bwrap_userns

pytestmark = [pytest.mark.integration]


@needs_bwrap_userns
def test_escape_probe_is_blocked(escape_probe: Path):
    """Run a hostile binary inside the sandbox and assert every escape attempt
    is blocked.

    The probe attempts:
    - mount() a tmpfs
    - unshare(CLONE_NEWUSER)
    - ptrace(TRACEME)
    - open a raw socket
    - connect() to localhost:1

    All five must fail. The probe writes BLOCKED/LEAKED lines and a SUMMARY
    line. We assert leaks=0 in the summary.
    """
    sb = select_sandbox()
    req = SandboxRequest(
        argv=["/sandbox/probe/escape_probe"],
        ro_mounts=[Mount(escape_probe.parent, Path("/sandbox/probe"), read_only=True)],
        limits=Limits(timeout_s=10, memory_bytes=128 * 1024 * 1024),
    )
    result = sb.run(req)
    out = result.stdout.decode(errors="replace")
    print(out)  # captured by pytest, useful when debugging
    assert "leaks=0" in out, f"sandbox leaked: {out}"
