from pathlib import Path

import pytest

from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.sandbox.detect import select_sandbox
from tests.conftest import needs_bwrap_userns

pytestmark = [pytest.mark.integration]


# Every escape the probe attempts, by the name it prints. A tuple means the
# attempt can be reported under either name -- the socket() may fail before
# connect() is reached, which is still a block, just an earlier one.
EXPECTED_ATTEMPTS: tuple[tuple[str, ...], ...] = (
    ("mount",),
    ("unshare(CLONE_NEWUSER)",),
    ("ptrace",),
    ("raw_socket",),
    ("connect_lo:1", "stream_socket"),
    ("bpf(BPF_PROG_LOAD)",),
    ("keyctl",),
)


@needs_bwrap_userns
def test_escape_probe_is_blocked(escape_probe: Path):
    """Run a hostile binary inside the sandbox and assert every escape attempt
    is blocked.

    `leaks=0` alone is not the assertion, because it is also what a probe that
    attempted NOTHING prints: `SUMMARY blocked=0 leaks=0` passes an `in` check
    for "leaks=0" just as happily as a fully exercised run. So the attempts
    themselves are asserted -- each one must appear as BLOCKED by name, and the
    count must match.

    That is also what keeps this list honest. The docstring here used to say
    "the probe attempts" five things and "all five must fail"; the probe had
    grown bpf(BPF_PROG_LOAD) and keyctl since, and nothing noticed, because
    nothing read the count.
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

    missing = [
        names[0]
        for names in EXPECTED_ATTEMPTS
        if not any(f"BLOCKED {n}" in out for n in names)
    ]
    assert not missing, f"these escapes were never attempted or were not blocked: {missing}\n{out}"
    assert f"blocked={len(EXPECTED_ATTEMPTS)} " in out + " ", (
        f"the probe reported a different number of blocks than the {len(EXPECTED_ATTEMPTS)} "
        f"attempts this test knows about — has escape_probe.c grown one?\n{out}"
    )
    assert "leaks=0" in out, f"sandbox leaked: {out}"
