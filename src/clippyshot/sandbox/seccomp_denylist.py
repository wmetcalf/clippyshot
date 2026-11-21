"""Shared source of truth for the sandbox syscall denylist + the BPF program bwrap installs.

The **nsjail** backend applies this denylist as a KAFEL policy
(``deploy/seccomp/clippyshot.seccomp.policy``); the **bwrap** backend has no KAFEL, so it
is handed an equivalent classic-BPF program built here and fed to ``bwrap --seccomp <fd>``.
Keeping the list in one place — plus a parity test asserting it equals the KAFEL ``ERRNO(1)``
block — stops the two backends from drifting.

**Policy:** ``DEFAULT ALLOW`` + ``ERRNO(1)`` (return EPERM) on the dangerous syscalls below;
any syscall made under a foreign ABI (i386 ``int 0x80``, x32) kills the process, as libseccomp
and kafel both do for a native-only filter. **x86_64-only**, matching the nsjail policy.

**Why no libseccomp.** The program used to be built with the libseccomp Python bindings, which
exist only as a distro package (``python3-seccomp``), never on PyPI. The shipped image installs
ClippyShot into a venv that cannot see distro packages, so the import failed there, bwrap got
no filter at all, and nothing noticed (it only marked itself insecure). The program is a flat
list of compares, so it is assembled directly from the syscall numbers below; libseccomp
remains an optional test oracle (tests/unit/test_seccomp_fail_closed.py), not a runtime dependency.

**Mount surface.** Besides ``mount``/``umount2``/``pivot_root`` the list denies ``chroot``/``fchroot``
and the whole new mount API (``fsopen``/``fsconfig``/``fsmount``/``fspick``/``move_mount``/
``open_tree``/``open_tree_attr``/``mount_setattr``). Without them a child that ever holds
CAP_SYS_ADMIN in its namespace can build or reflag a mount without calling mount(2), for example
clearing ``ro`` on one of the binds the backends rely on. The docker JSON profile
(``deploy/seccomp/clippyshot.seccomp.json``) is an allowlist and denies them by omission. This
mirrors blastbox's worker denylist (blastbox #216, released in 0.1.51).
"""
from __future__ import annotations

import errno
import logging
import platform
import struct

_log = logging.getLogger("clippyshot.sandbox.seccomp")

# EXACTLY the KAFEL clippyshot_deny ERRNO(1) block (deploy/seccomp/clippyshot.seccomp.policy).
# A parity test guards this equality so the bwrap BPF and the nsjail KAFEL policy can't drift.
DENY_ERRNO1: tuple[str, ...] = (
    # Kernel module loading
    "init_module", "finit_module", "delete_module",
    # Key management
    "add_key", "keyctl", "request_key",
    # Mount / filesystem manipulation. `umount2` is x86_64 syscall 166; the bundled kafel names
    # that same number `umount` (it can't lex `umount2`) -- the parity test normalizes it.
    "mount", "umount2", "pivot_root", "chroot", "fchroot",
    # The new mount API (Linux 5.2+) builds and attaches mounts without mount(2): fsopen ->
    # fsconfig -> fsmount -> move_mount; open_tree/open_tree_attr clone a subtree; fspick
    # reconfigures an existing mount; mount_setattr flips a mount's flags (e.g. clears `ro`).
    # The KAFEL policy names these by NUMBER: nsjail's bundled kafel predates the names.
    "fsopen", "fsconfig", "fsmount", "fspick", "move_mount",
    "open_tree", "open_tree_attr", "mount_setattr",
    # Kernel exec / reboot
    "kexec_load", "kexec_file_load", "reboot",
    # Process inspection/control of others
    "ptrace", "process_vm_readv", "process_vm_writev",
    # BPF / perf / tracing
    "bpf", "perf_event_open",
    # io_uring — prolific attack surface; LO doesn't use it. By NUMBER in the KAFEL policy too.
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


# x86_64 syscall numbers (arch/x86/entry/syscalls/syscall_64.tbl) for every name above.
# The new-mount-API calls, io_uring, open_tree_attr and fchroot sit in the unified asm-generic
# range (>= 424), which is the same table on every arch. A test cross-checks this table against
# the kernel headers and, where it is installed, libseccomp.
X86_64_NR: dict[str, int] = {
    "init_module": 175, "finit_module": 313, "delete_module": 176,
    "add_key": 248, "keyctl": 250, "request_key": 249,
    "mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "fchroot": 472,
    "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "move_mount": 429,
    "open_tree": 428, "open_tree_attr": 467, "mount_setattr": 442,
    "kexec_load": 246, "kexec_file_load": 320, "reboot": 169,
    "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
    "bpf": 321, "perf_event_open": 298,
    "io_uring_setup": 425, "io_uring_enter": 426, "io_uring_register": 427,
    "userfaultfd": 323,
    "clock_settime": 227, "clock_adjtime": 305, "settimeofday": 164, "adjtimex": 159,
    "sethostname": 170, "setdomainname": 171,
    "unshare": 272,
    "quotactl": 179, "vhangup": 153, "nfsservctl": 180,
    "name_to_handle_at": 303, "open_by_handle_at": 304, "swapon": 167, "swapoff": 168,
    "kcmp": 312,
    "uselib": 134, "sysfs": 139,
    "iopl": 172, "ioperm": 173, "modify_ldt": 154,
    "fanotify_init": 300, "fanotify_mark": 301,
}

# struct seccomp_data offsets, classic-BPF opcodes and seccomp return values (linux/seccomp.h,
# linux/filter.h, linux/audit.h).
_OFF_NR, _OFF_ARCH = 0, 4
_LD_W_ABS = 0x20          # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15             # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35             # BPF_JMP | BPF_JGE | BPF_K
_RET_K = 0x06             # BPF_RET | BPF_K
AUDIT_ARCH_X86_64 = 0xC000003E
_X32_SYSCALL_BIT = 0x40000000
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def assemble_denylist(nrs: list[int]) -> bytes:
    """A ``DEFAULT ALLOW`` program returning EPERM for each of ``nrs`` (x86_64 numbers).

    Layout::

        0  ld  arch
        1  jeq AUDIT_ARCH_X86_64 ? 3 : 2
        2  ret KILL_PROCESS                     foreign ABI (i386 int 0x80)
        3  ld  nr
        4  jeq -1 ? ALLOW : 5                   syscall(-1) is ENOSYS, as libseccomp allows it
        5  jge X32_SYSCALL_BIT ? KILL : 6       x32 ABI
        6+ jeq nr_i ? ERRNO : next              one per denied syscall
        .  ret ALLOW
        .  ret ERRNO|EPERM                       ERRNO
        .  ret KILL_PROCESS                      KILL (cBPF jumps only forward)
    """
    uniq = sorted(set(nrs))
    n = len(uniq)
    if not 0 < n < 250:   # jt/jf are 8-bit; keep every jump in range
        raise ValueError(f"denylist size {n} out of range")
    prog = [
        _insn(_LD_W_ABS, 0, 0, _OFF_ARCH),
        _insn(_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),
        _insn(_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        _insn(_LD_W_ABS, 0, 0, _OFF_NR),
        # To the ALLOW: past the x32 check and the n compares.
        _insn(_JEQ_K, n + 1, 0, 0xFFFFFFFF),
        # To the trailing KILL: past the n compares and the ALLOW and ERRNO returns.
        _insn(_JGE_K, n + 2, 0, _X32_SYSCALL_BIT),
    ]
    for i, nr in enumerate(uniq):
        # Distance from this compare to the ERRNO return: the remaining compares + the ALLOW.
        prog.append(_insn(_JEQ_K, (n - 1 - i) + 1, 0, nr))
    prog.append(_insn(_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    prog.append(_insn(_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM))
    prog.append(_insn(_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS))
    return b"".join(prog)


def build_bpf_bytes() -> bytes | None:
    """The ``DEFAULT ALLOW`` + ``ERRNO(1)``-denylist BPF program bwrap is handed via
    ``--seccomp <fd>``, or ``None`` where no correct program can be built.

    ``None`` on a non-x86_64 host (the numbers are x86_64's) and if any name lacks a number --
    never a partial denylist. The bwrap backend then refuses to run (it fails closed; see
    ``BwrapSandbox``) rather than run without the filter.
    """
    if platform.machine() not in ("x86_64", "amd64"):
        _log.warning("seccomp: denylist is x86_64-only; not building on %s", platform.machine())
        return None
    missing = [n for n in DENY_ERRNO1 if n not in X86_64_NR]
    if missing:
        _log.error("seccomp: no x86_64 number for %s -- refusing a partial denylist", missing)
        return None
    return assemble_denylist([X86_64_NR[n] for n in DENY_ERRNO1])
