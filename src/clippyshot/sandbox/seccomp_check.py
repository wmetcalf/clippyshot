"""Prove that a namespace backend's seccomp filter is actually enforced on its children.

Neither nsjail nor bwrap says whether the filter it was handed took effect, and both have
shipped without one: the image's bundled kafel rejected ``io_uring_setup`` so nsjail could not
compile the policy, and bwrap's BPF builder could not import its bindings in the image's venv
so bwrap got no filter. Configuration ("the policy file exists", "the import worked") is not
evidence. This module runs a probe INSIDE the sandbox and checks the kernel's answers.

The probe makes a few denylisted syscalls with harmless arguments, chosen so that WITHOUT the
filter an unprivileged caller gets something other than EPERM wherever the kernel allows the
call at all (``unshare(0)`` is a no-op that succeeds; ``open_tree(-1, "", 0)`` is ENOENT;
``kcmp(self, self, KCMP_VM)`` is 0; ``io_uring_setup(0, NULL)`` is EFAULT). It also reports
``Seccomp_filters`` from ``/proc/self/status``.

A verdict is OK only when

1. every probed call returns EPERM inside the sandbox, AND
2. the EPERM is attributable to the sandbox, not to an outer layer (a Docker profile can deny
   the same calls): at least one call is NOT EPERM when the same probe runs outside the
   sandbox. A higher ``Seccomp_filters`` count inside is NOT accepted instead: an allow-all
   filter raises it too.

Anything else -- the probe did not run, a call got through, or the two cannot be told apart --
is a failed verdict, and the backends refuse to run (``SeccompNotEnforced``). There is no
override: ``CLIPPYSHOT_WARN_ON_INSECURE`` is set routinely on gVisor tiers for the container
backend's hardening checks, so letting it also waive this filter would make the filter silently
optional again.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from clippyshot.errors import SandboxUnavailable
from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest
from clippyshot.types import SandboxResult

_log = logging.getLogger("clippyshot.sandbox.seccomp")

# The interpreter the probe runs under, inside the sandbox. The system python3, not the venv's:
# the backends bind /usr read-only into the jail, but not /opt or a checkout.
PROBE_PYTHON = "/usr/bin/python3"
_PROBE_TIMEOUT_S = 30

# Printed as one JSON line. Arch-specific numbers for the calls outside the unified range.
PROBE_SOURCE = r"""
import ctypes, json, os, platform
nr = {
    "x86_64": {"mount": 165, "unshare": 272, "kcmp": 312},
    "aarch64": {"mount": 40, "unshare": 97, "kcmp": 272},
}.get(platform.machine())
out = {"arch": platform.machine(), "calls": None, "filters": None}
if nr is not None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    pid = os.getpid()
    calls = {
        "unshare": (nr["unshare"], 0),
        "open_tree": (428, -1, b"", 0),
        "kcmp": (nr["kcmp"], pid, pid, 1, 0, 0),
        "io_uring_setup": (425, 0, None),
        "mount": (nr["mount"], None, None, None, 0, None),
    }
    res = {}
    for name, (n, *args) in calls.items():
        ctypes.set_errno(0)
        r = libc.syscall(n, *args)
        res[name] = "ok" if r >= 0 else ctypes.get_errno()
    out["calls"] = res
try:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("Seccomp_filters:"):
                out["filters"] = int(line.split(":", 1)[1])
except (OSError, ValueError):
    pass
print(json.dumps(out))
"""


class SeccompNotEnforced(SandboxUnavailable):
    """A namespace backend's seccomp filter is missing or not enforced; it refuses to run."""


@dataclass(frozen=True)
class SeccompVerdict:
    ok: bool
    detail: str


def probe_request() -> SandboxRequest:
    """The probe as a sandbox request. No soffice AppArmor profile: it is python, not soffice
    (the profile would deny it), and seccomp is independent of AppArmor."""
    return SandboxRequest(
        argv=[PROBE_PYTHON, "-I", "-S", "-c", PROBE_SOURCE],
        limits=Limits(timeout_s=_PROBE_TIMEOUT_S),
        attach_apparmor=False,
    )


def parse_probe(stdout: bytes) -> dict | None:
    """The probe's JSON report, or None if the output is not one."""
    lines = stdout.decode(errors="replace").strip().splitlines()
    if not lines:
        return None
    try:
        got = json.loads(lines[-1])
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def evaluate(inside: dict | None, outside: dict | None) -> SeccompVerdict:
    """Judge the probe reports from inside and outside the sandbox (see the module doc)."""
    if inside is None:
        return SeccompVerdict(False, "the probe produced no report inside the sandbox")
    calls = inside.get("calls")
    if not isinstance(calls, dict) or not calls:
        return SeccompVerdict(False, f"no probe calls for arch {inside.get('arch')!r}")
    leaked = {k: v for k, v in calls.items() if v != errno.EPERM}
    if leaked:
        return SeccompVerdict(
            False, f"denied syscalls were not EPERM inside the sandbox ('ok' = succeeded): "
                   f"{leaked}")
    out_calls = (outside or {}).get("calls")
    if isinstance(out_calls, dict):
        own = sorted(k for k, v in out_calls.items() if v != errno.EPERM and k in calls)
        if own:
            return SeccompVerdict(
                True, f"EPERM inside, allowed outside: {', '.join(own)}")
    # No fallback to "more seccomp filters inside": an allow-all filter raises that count too,
    # so under an outer profile that already denies every probed call it would pass a sandbox
    # whose own filter denies nothing. Such an outer profile also blocks the namespace setup
    # nsjail/bwrap need (unshare), so refusing there costs nothing real.
    f_in, f_out = inside.get("filters"), (outside or {}).get("filters")
    return SeccompVerdict(
        False, "probed calls are EPERM inside, but they are EPERM outside too "
               f"(seccomp filters outside={f_out}, inside={f_in}): cannot tell the sandbox's "
               "own filter from an outer one")


_baseline_lock = threading.Lock()
_baseline: dict | None = None
_baseline_done = False


def outside_report() -> dict | None:
    """The probe run directly, unsandboxed: once per process once it succeeds (a failed run is
    retried next time rather than leaving the baseline empty for good)."""
    global _baseline, _baseline_done
    with _baseline_lock:
        if not _baseline_done:
            try:
                r = subprocess.run(
                    [PROBE_PYTHON, "-I", "-S", "-c", PROBE_SOURCE],
                    capture_output=True, timeout=_PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL,
                )
                _baseline = parse_probe(r.stdout) if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                _baseline = None
            _baseline_done = _baseline is not None
        return _baseline


# Successful verdicts by (backend, binary, filter digest). Only successes are cached, like the
# AppArmor attach probe: a host that fails keeps being re-probed until it is fixed.
_VERIFIED: dict[tuple[str, str, str], SeccompVerdict] = {}


def filter_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(
    key: tuple[str, str, str],
    run_unchecked: Callable[[SandboxRequest], SandboxResult],
) -> SeccompVerdict:
    """Run the probe through ``run_unchecked`` (the backend's run WITHOUT this gate) and judge it."""
    hit = _VERIFIED.get(key)
    if hit is not None:
        return hit
    try:
        res = run_unchecked(probe_request())
    except Exception as exc:  # noqa: BLE001 - any failure to run the probe is a failed verdict
        return SeccompVerdict(False, f"the probe could not run in the sandbox: {exc}")
    if res.exit_code != 0 or res.killed:
        tail = res.stderr.decode(errors="replace").strip()[-300:]
        return SeccompVerdict(
            False, f"the probe failed in the sandbox (exit={res.exit_code}, "
                   f"killed={res.killed}): {tail}")
    verdict = evaluate(parse_probe(res.stdout), outside_report())
    if verdict.ok:
        _VERIFIED[key] = verdict
    return verdict
