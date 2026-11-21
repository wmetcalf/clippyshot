"""The inner sandboxes' seccomp filter is verified, not assumed, and a missing one fails closed.

Shipped defect: inside the ClippyShot image neither namespace backend had a working filter.
nsjail's bundled kafel rejected `io_uring_setup`, so the KAFEL policy never compiled (nsjail
exited 255 on every run while the backend still reported `seccomp_active`); bwrap's BPF
builder imported libseccomp bindings the image's venv cannot see, so bwrap had no filter.

These tests pin, without needing the image:

* the hand-assembled BPF decides every syscall exactly as libseccomp would (when the system
  python carries libseccomp's bindings) and uses the kernel's numbers;
* the verdict logic (`seccomp_check.evaluate`) accepts only an EPERM it can attribute to the
  sandbox;
* both backends refuse to run when the filter is missing or its probe fails, including under
  CLIPPYSHOT_WARN_ON_INSECURE.

The in-image proof (real nsjail + bwrap in the built image) is tests/docker/test_inner_seccomp.py.
"""
from __future__ import annotations

import errno
import platform
import re
import subprocess
from pathlib import Path

import pytest

from clippyshot.sandbox import seccomp_check, seccomp_denylist
from clippyshot.sandbox.seccomp_check import SeccompNotEnforced, SeccompVerdict, evaluate
from clippyshot.types import SandboxResult
from tests.unit.test_seccomp_mount_api import _run_cbpf

x86_64_only = pytest.mark.skipif(
    platform.machine() not in ("x86_64", "amd64"), reason="the BPF denylist is x86_64-only")

ARCH_X86_64, ARCH_I386, ARCH_AARCH64 = 0xC000003E, 0x40000003, 0xC00000B7
ALLOW, ERRNO_EPERM = 0x7FFF0000, 0x00050000 | errno.EPERM
KILLS = (0x00000000, 0x80000000)   # KILL_THREAD (libseccomp's default), KILL_PROCESS (ours)


def _decision(ret: int) -> str:
    return "kill" if ret in KILLS else hex(ret)


# --- the BPF program ----------------------------------------------------------------------


def test_every_denied_name_has_a_number() -> None:
    assert set(seccomp_denylist.DENY_ERRNO1) <= set(seccomp_denylist.X86_64_NR)


@pytest.mark.skipif(not Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h").exists(),
                    reason="no x86_64 kernel headers")
def test_the_numbers_match_the_kernel_headers() -> None:
    hdr = Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h").read_text()
    kernel = {m[0]: int(m[1]) for m in re.findall(r"#define __NR_(\w+) (\d+)", hdr)}
    wrong = {n: (nr, kernel[n]) for n, nr in seccomp_denylist.X86_64_NR.items()
             if n in kernel and kernel[n] != nr}
    assert not wrong, f"(ours, kernel): {wrong}"


@x86_64_only
def test_the_program_denies_exactly_the_list_and_allows_the_rest() -> None:
    bpf = seccomp_denylist.build_bpf_bytes()
    assert bpf is not None
    denied = set(seccomp_denylist.X86_64_NR[n] for n in seccomp_denylist.DENY_ERRNO1)
    for nr in range(0, 600):
        want = ERRNO_EPERM if nr in denied else ALLOW
        assert _run_cbpf(bpf, nr, ARCH_X86_64) == want, nr


@x86_64_only
def test_foreign_abis_are_killed_not_allowed() -> None:
    """A denylist that only checked numbers would let `int 0x80` (i386 numbering, where 165 is
    not mount) or the x32 ABI (nr | 0x40000000) walk straight past it."""
    bpf = seccomp_denylist.build_bpf_bytes()
    assert bpf is not None
    assert _decision(_run_cbpf(bpf, 21, ARCH_I386)) == "kill"         # i386 mount
    assert _decision(_run_cbpf(bpf, 40, ARCH_AARCH64)) == "kill"
    assert _decision(_run_cbpf(bpf, 0x40000000 | 165, ARCH_X86_64)) == "kill"
    assert _run_cbpf(bpf, -1, ARCH_X86_64) == ALLOW                   # syscall(-1): ENOSYS


_LIBSECCOMP_EXPORT = r"""
import os, sys, seccomp
f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
for nr in map(int, sys.argv[1:]):
    f.add_rule(seccomp.ERRNO(1), nr)
fd = os.memfd_create("oracle")
fo = os.fdopen(fd, "wb", closefd=False)
f.export_bpf(fo)
fo.flush()
os.lseek(fd, 0, 0)
sys.stdout.buffer.write(os.read(fd, 1 << 20))
"""


def _libseccomp_bpf(nrs: list[int]) -> bytes:
    # The bindings are a distro package; the system python may have them where a venv cannot.
    py = "/usr/bin/python3"
    if not Path(py).exists():
        pytest.skip("no system python3")
    r = subprocess.run([py, "-I", "-c", _LIBSECCOMP_EXPORT, *map(str, nrs)],
                       capture_output=True, timeout=30)
    if r.returncode != 0:
        pytest.skip("the system python has no libseccomp bindings (python3-seccomp)")
    return r.stdout


@x86_64_only
def test_the_program_decides_like_libseccomp() -> None:
    """Independent oracle: libseccomp's own program for the same rules, evaluated for every
    syscall number on the native arch, a foreign arch, and the x32 range."""
    nrs = [seccomp_denylist.X86_64_NR[n] for n in seccomp_denylist.DENY_ERRNO1]
    lib = _libseccomp_bpf(nrs)
    ours = seccomp_denylist.build_bpf_bytes()
    assert ours is not None
    numbers = [*range(0, 600), -1, *(0x40000000 | i for i in (0, 165, 272, 512))]
    diff = [
        (hex(arch), nr)
        for arch in (ARCH_X86_64, ARCH_I386, ARCH_AARCH64)
        for nr in numbers
        if _decision(_run_cbpf(lib, nr, arch)) != _decision(_run_cbpf(ours, nr, arch))
    ]
    assert not diff, diff[:10]


_X32 = 0x40000000
# x32-only numbers (arch/x86/entry/syscalls/syscall_64.tbl, 512+) for denied calls whose
# native number is 64-bit-ABI-only.
_X32_ONLY = {"ptrace": 521, "kexec_load": 528, "process_vm_readv": 539, "process_vm_writev": 540}


def test_the_kafel_policy_denies_every_call_under_the_x32_abi_too() -> None:
    """kafel checks only AUDIT_ARCH, which x32 shares with x86_64, and compares exact numbers,
    so `mount | 0x40000000` sails past a native-only denylist on an X32-enabled kernel."""
    policy = (Path(__file__).resolve().parents[2] / "deploy/seccomp/clippyshot.seccomp.policy")
    text = policy.read_text()
    x32 = text[text.index("POLICY clippyshot_deny_x32"):]
    got = {int(m, 0) for m in re.findall(r"SYSCALL\[(0x[0-9a-fA-F]+|\d+)\]", x32)}
    want = {_X32 | seccomp_denylist.X86_64_NR[n] for n in seccomp_denylist.DENY_ERRNO1}
    want |= {_X32 | nr for nr in _X32_ONLY.values()}
    assert want <= got, sorted(hex(n) for n in want - got)
    assert "USE clippyshot_deny_x32" in text   # and it is actually in force


# --- the verdict --------------------------------------------------------------------------

EPERM = errno.EPERM
ALL_EPERM = {"unshare": EPERM, "open_tree": EPERM, "kcmp": EPERM, "io_uring_setup": EPERM,
             "mount": EPERM}
UNFILTERED = {"unshare": "ok", "open_tree": errno.ENOENT, "kcmp": "ok",
              "io_uring_setup": errno.EFAULT, "mount": errno.EFAULT}


def _report(calls, filters=None):
    return {"arch": "x86_64", "calls": calls, "filters": filters}


def test_eperm_inside_and_allowed_outside_is_verified() -> None:
    v = evaluate(_report(ALL_EPERM, 1), _report(UNFILTERED, 0))
    assert v.ok, v.detail


def test_a_call_that_gets_through_fails() -> None:
    inside = dict(ALL_EPERM, unshare="ok")
    v = evaluate(_report(inside, 1), _report(UNFILTERED, 0))
    assert not v.ok and "unshare" in v.detail


def test_an_unfiltered_sandbox_fails() -> None:
    assert not evaluate(_report(UNFILTERED, 0), _report(UNFILTERED, 0)).ok


def test_eperm_from_an_outer_layer_alone_is_not_the_sandbox_filter() -> None:
    """A Docker profile can deny the very same calls. EPERM on both sides with no extra filter
    inside proves nothing about the sandbox's own filter."""
    v = evaluate(_report(ALL_EPERM, 1), _report(ALL_EPERM, 1))
    assert not v.ok and "cannot tell" in v.detail
    assert not evaluate(_report(ALL_EPERM, None), _report(ALL_EPERM, None)).ok


def test_an_extra_filter_inside_does_not_attribute_the_eperm() -> None:
    """An allow-all inner filter raises Seccomp_filters exactly like a real one, so under an
    outer profile that already denies every probed call the count proves nothing."""
    assert not evaluate(_report(ALL_EPERM, 2), _report(ALL_EPERM, 1)).ok


def test_no_report_or_no_calls_fails() -> None:
    assert not evaluate(None, _report(UNFILTERED)).ok
    assert not evaluate({"arch": "riscv64", "calls": None, "filters": 1}, _report(UNFILTERED)).ok


# --- the gate on both backends ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(seccomp_check, "_VERIFIED", {})


def _bwrap(monkeypatch):
    from clippyshot.sandbox import bwrap

    monkeypatch.setattr(bwrap.shutil, "which",
                        lambda n: None if n == "aa-exec" else "/usr/bin/bwrap")
    monkeypatch.setattr(bwrap, "_probe_bwrap_cgroup_pids", lambda _p: True)
    monkeypatch.setattr(seccomp_denylist, "build_bpf_bytes", lambda: b"\x06" + b"\x00" * 7)
    return bwrap.BwrapSandbox(bwrap_path="/usr/bin/bwrap")


def _nsjail(monkeypatch, tmp_path, *, policy: bool = True):
    from clippyshot.sandbox import nsjail

    fake = tmp_path / "nsjail"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    pol = tmp_path / "p.policy"
    pol.write_text("POLICY p { } USE p DEFAULT ALLOW\n")
    monkeypatch.setattr(nsjail, "_probe_nsjail_proc_apparmor", lambda _p: False)
    if not policy:
        monkeypatch.setattr(nsjail, "_find_seccomp_policy", lambda: None)
    return nsjail.NsjailSandbox(str(fake), seccomp_policy=pol if policy else None)


def _result(exit_code=0, stdout=b"", stderr=b""):
    return SandboxResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=1,
                         killed=False)


@pytest.mark.parametrize("make", ["bwrap", "nsjail"])
@pytest.mark.parametrize("insecure_ok", ["", "1"])
def test_a_failed_probe_refuses_every_run(monkeypatch, tmp_path, make, insecure_ok) -> None:
    """The probe runs FIRST. If it shows the filter is not enforced -- here nsjail's own
    exit 255 for an uncompilable policy -- the real request never runs, the backend turns
    insecure, and CLIPPYSHOT_WARN_ON_INSECURE does not waive it."""
    if insecure_ok:
        monkeypatch.setenv("CLIPPYSHOT_WARN_ON_INSECURE", insecure_ok)
    sb = _bwrap(monkeypatch) if make == "bwrap" else _nsjail(monkeypatch, tmp_path)
    ran: list[list[str]] = []

    def fake_run(req):
        ran.append(list(req.argv))
        return _result(255, stderr=b"[F] main():360 Couldn't prepare sandboxing policy")

    monkeypatch.setattr(sb, "_run", fake_run)
    from clippyshot.sandbox.base import SandboxRequest

    req = SandboxRequest(argv=["/usr/bin/soffice"])
    with pytest.raises(SeccompNotEnforced, match="Couldn't prepare sandboxing policy"):
        sb.run(req)
    with pytest.raises(SeccompNotEnforced):
        sb.smoketest()
    with pytest.raises(SeccompNotEnforced):
        sb.spawn(req)
    # Only probes ran -- one per attempt, since a failure is re-probed -- never the request.
    assert ran and all(argv[0] == seccomp_check.PROBE_PYTHON for argv in ran)
    assert sb.seccomp_active is False and sb.seccomp_source == "none"
    assert "seccomp_not_enforced" in sb.insecurity_reasons and not sb.secure


def test_nsjail_without_a_policy_refuses(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CLIPPYSHOT_WARN_ON_INSECURE", "1")
    sb = _nsjail(monkeypatch, tmp_path, policy=False)
    monkeypatch.setattr(sb, "_run", lambda req: pytest.fail("ran without a policy"))
    from clippyshot.sandbox.base import SandboxRequest

    with pytest.raises(SeccompNotEnforced, match="no KAFEL policy"):
        sb.run(SandboxRequest(argv=["/bin/true"]))
    assert sb.seccomp_active is False


@pytest.mark.parametrize("make", ["bwrap", "nsjail"])
def test_a_passing_probe_is_run_once_then_requests_run(monkeypatch, tmp_path, make) -> None:
    import json

    sb = _bwrap(monkeypatch) if make == "bwrap" else _nsjail(monkeypatch, tmp_path)
    monkeypatch.setattr(seccomp_check, "outside_report", lambda: _report(UNFILTERED, 0))
    ran: list[str] = []

    def fake_run(req):
        ran.append(req.argv[0])
        if req.argv[0] == seccomp_check.PROBE_PYTHON:
            return _result(0, json.dumps(_report(ALL_EPERM, 1)).encode())
        return _result(0)

    monkeypatch.setattr(sb, "_run", fake_run)
    from clippyshot.sandbox.base import SandboxRequest

    sb.run(SandboxRequest(argv=["/bin/true"]))
    sb.run(SandboxRequest(argv=["/bin/echo"]))
    assert ran == [seccomp_check.PROBE_PYTHON, "/bin/true", "/bin/echo"]
    assert sb.seccomp_active
    assert not {"seccomp_missing", "seccomp_not_enforced"} & set(sb.insecurity_reasons)
    assert isinstance(sb.seccomp_verdict, SeccompVerdict) and sb.seccomp_verdict.ok


@pytest.mark.parametrize("make", ["bwrap", "nsjail"])
def test_a_transient_probe_failure_is_retried_not_final(monkeypatch, tmp_path, make) -> None:
    """A probe that times out once on a loaded host must not disable the backend for the life
    of the process: the next run re-probes, and a pass restores it."""
    import json

    sb = _bwrap(monkeypatch) if make == "bwrap" else _nsjail(monkeypatch, tmp_path)
    monkeypatch.setattr(seccomp_check, "outside_report", lambda: _report(UNFILTERED, 0))
    probes = iter([_result(-9, stderr=b"timeout"),
                   _result(0, json.dumps(_report(ALL_EPERM, 1)).encode())])
    ran: list[str] = []

    def fake_run(req):
        ran.append(req.argv[0])
        return next(probes) if req.argv[0] == seccomp_check.PROBE_PYTHON else _result(0)

    monkeypatch.setattr(sb, "_run", fake_run)
    from clippyshot.sandbox.base import SandboxRequest

    with pytest.raises(SeccompNotEnforced):
        sb.run(SandboxRequest(argv=["/bin/true"]))
    assert "seccomp_not_enforced" in sb.insecurity_reasons
    sb.run(SandboxRequest(argv=["/bin/true"]))
    assert ran == [seccomp_check.PROBE_PYTHON, seccomp_check.PROBE_PYTHON, "/bin/true"]
    assert sb.seccomp_active and "seccomp_not_enforced" not in sb.insecurity_reasons
