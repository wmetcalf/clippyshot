"""Shared source of truth for the sandbox syscall denylist + a libseccomp BPF builder.

The **nsjail** backend applies this denylist as a KAFEL policy
(``deploy/seccomp/clippyshot.seccomp.policy``); the **bwrap** backend has no KAFEL, so it
builds an equivalent BPF program here (via ``python3-libseccomp``) and feeds it to
``bwrap --seccomp <fd>``. Keeping the list in one place — plus a parity test asserting it
equals the KAFEL ``ERRNO(1)`` block — stops the two backends from drifting.

**Policy:** ``DEFAULT ALLOW`` + ``ERRNO(1)`` (return EPERM) on the dangerous syscalls below.
**x86_64-only**, matching the nsjail policy (syscall availability is arch-specific).
``python3-libseccomp`` is a *distro* package (not on PyPI); where it's absent the builder
returns ``None`` and the bwrap backend keeps marking itself insecure on the seccomp axis
(fail-safe — same as before this module existed).
"""
from __future__ import annotations

import logging
import os
import platform

_log = logging.getLogger("clippyshot.sandbox.seccomp")

# EXACTLY the KAFEL clippyshot_deny ERRNO(1) block (deploy/seccomp/clippyshot.seccomp.policy).
# A parity test guards this equality so the bwrap BPF and the nsjail KAFEL policy can't drift.
DENY_ERRNO1: tuple[str, ...] = (
    # Kernel module loading
    "init_module", "finit_module", "delete_module",
    # Key management
    "add_key", "keyctl", "request_key",
    # Mount / filesystem manipulation
    "mount", "umount", "pivot_root",
    # Kernel exec / reboot
    "kexec_load", "kexec_file_load", "reboot",
    # Process inspection/control of others
    "ptrace", "process_vm_readv", "process_vm_writev",
    # BPF / perf / tracing
    "bpf", "perf_event_open",
    # io_uring — prolific attack surface; LO doesn't use it
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    # Userfaultfd (fault-injection primitives)
    "userfaultfd",
    # Clock manipulation
    "clock_settime", "clock_adjtime", "settimeofday", "adjtimex",
    # Host identity
    "sethostname", "setdomainname",
    # Namespace creation (we're already namespaced)
    "unshare",
    # Dangerous system controls
    "quotactl", "vhangup", "nfsservctl",
    "name_to_handle_at", "open_by_handle_at", "swapon", "swapoff",
    # Obscure cross-task primitives
    "kcmp",
    # Legacy / deprecated
    "uselib", "sysfs",
    # x86 port-IO and local descriptor table
    "iopl", "ioperm", "modify_ldt",
    # fanotify — privileged filesystem notification
    "fanotify_init", "fanotify_mark",
)


def build_bpf_bytes() -> bytes | None:
    """Build a ``DEFAULT ALLOW`` + ``ERRNO(1)``-denylist BPF program via libseccomp and return
    its raw bytes (to feed ``bwrap --seccomp <fd>``).

    Returns ``None`` — so the caller keeps bwrap marked insecure — when the arch isn't x86_64,
    libseccomp is unavailable, OR **any** deny rule can't be added. That last case is deliberately
    **fail-CLOSED**: a partial denylist (e.g. an older libseccomp missing a syscall name) would
    still report ``seccomp_active=True`` while silently allowing a denied syscall, breaking the
    parity guarantee — attach the FULL filter or none at all.
    """
    if platform.machine() not in ("x86_64", "amd64"):
        # The denylist (and the nsjail KAFEL policy it mirrors) is x86_64-only — syscall
        # availability is arch-specific, so don't claim a filter on other arches.
        _log.warning("seccomp: denylist is x86_64-only; not building on %s", platform.machine())
        return None
    try:
        import seccomp  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
        for name in DENY_ERRNO1:
            try:
                f.add_rule(seccomp.ERRNO(1), name)
            except Exception as e:  # noqa: BLE001
                _log.warning("seccomp: cannot add deny rule for %r (%s) — failing closed", name, e)
                return None  # fail-closed: no partial denylist
        # Export to a memfd (a pipe could deadlock if the BPF exceeded the pipe buffer; it won't
        # here, but memfd is unconditionally safe). closefd=False so the outer os.close owns the fd.
        fd = os.memfd_create("clippyshot_seccomp_build", 0)
        try:
            with os.fdopen(fd, "wb", closefd=False) as bf:
                f.export_bpf(bf)   # flushed + closed by the with (fd stays open, closefd=False)
            os.lseek(fd, 0, os.SEEK_SET)
            return b"".join(iter(lambda: os.read(fd, 1 << 16), b""))
        finally:
            os.close(fd)
    except Exception as e:  # noqa: BLE001
        _log.warning("seccomp: BPF build failed, bwrap will run without a syscall filter: %s", e)
        return None
