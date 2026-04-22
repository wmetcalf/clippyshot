"""Unit tests for the seccomp policy file.

The policy at deploy/seccomp/clippyshot.seccomp.policy uses a denylist
approach (DEFAULT ALLOW): explicitly listed dangerous syscalls are denied.
Everything else is allowed. This is less strict but more maintainable.

These tests verify:
1. The policy file exists and declares KAFEL POLICY/USE blocks
2. The policy uses DEFAULT ALLOW (denylist, not allowlist)
3. Dangerous syscalls ARE present in the deny block
4. Essential soffice syscalls ARE NOT present in the deny block
5. The explicit ERRNO(1) deny block has no duplicates

These tests do NOT require nsjail — they only parse the policy as text.
End-to-end verification lives in tests/integration/escape_probe.c.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

POLICY = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "seccomp"
    / "clippyshot.seccomp.policy"
)

# Syscalls that must NEVER be allowed (must be in deny block).
MUST_DENY = {
    "bpf", "keyctl", "add_key", "request_key",
    "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module",
    "ptrace", "process_vm_readv", "process_vm_writev",
    "mount", "umount", "pivot_root",
    "reboot", "settimeofday", "sethostname", "setdomainname",
    "iopl", "ioperm", "modify_ldt",
    "name_to_handle_at", "open_by_handle_at",
    "perf_event_open", "userfaultfd",
    "fanotify_init", "fanotify_mark",
}

# Syscalls that soffice needs — must NOT be in the deny block.
# Note: uname is newuname in KAFEL. glibc on this host also issues the
# legacy `fstat` syscall during dynamic-loader startup, which this KAFEL
# build exposes as `newfstat`.
MUST_NOT_DENY = {
    "read", "write", "open", "openat", "close", "mmap", "mprotect",
    "brk", "futex", "clone", "execve", "exit_group", "getpid",
    "rt_sigaction", "rt_sigprocmask", "fcntl", "dup", "dup2",
    "pipe", "pipe2", "newfstat", "newfstatat", "statx", "getdents64", "prlimit64",
    "clock_gettime", "nanosleep", "getrandom",
}


def _extract_deny_tokens() -> set[str]:
    """Extract all syscall identifiers from the ERRNO(1) { ... } block."""
    text = POLICY.read_text()
    match = re.search(r"ERRNO\(1\)\s*\{([^}]*)\}", text, re.DOTALL)
    if not match:
        return set()
    body = match.group(1)
    tokens = set()
    for line in body.splitlines():
        line = line.split("//", 1)[0].strip()  # strip comments
        for tok in line.split(","):
            tok = tok.strip()
            if tok and tok.isidentifier():
                tokens.add(tok)
    return tokens


def test_seccomp_policy_file_exists() -> None:
    assert POLICY.is_file(), f"seccomp policy file missing: {POLICY}"


def test_seccomp_policy_declares_kafel_blocks() -> None:
    text = POLICY.read_text()
    assert "POLICY " in text
    assert "USE " in text


def test_seccomp_policy_uses_default_allow() -> None:
    """The policy must use DEFAULT ALLOW (denylist)."""
    text = POLICY.read_text()
    assert "DEFAULT ALLOW" in text, (
        "policy does not use DEFAULT ALLOW — it should be a denylist"
    )


@pytest.mark.parametrize("syscall", sorted(MUST_DENY))
def test_dangerous_syscall_in_deny_block(syscall: str) -> None:
    """Dangerous syscalls MUST appear in the deny block."""
    tokens = _extract_deny_tokens()
    assert syscall in tokens, (
        f"{syscall!r} not found in deny block — this is a dangerous syscall "
        "that MUST be denied"
    )


@pytest.mark.parametrize("syscall", sorted(MUST_NOT_DENY))
def test_essential_syscall_not_in_deny_block(syscall: str) -> None:
    """Essential soffice syscalls must NOT be present in the deny block."""
    tokens = _extract_deny_tokens()
    assert syscall not in tokens, (
        f"{syscall!r} found in deny block — soffice needs this syscall"
    )


def test_no_duplicate_syscalls_in_deny_block() -> None:
    """Catch typos that manifest as duplicated syscall names."""
    text = POLICY.read_text()
    match = re.search(r"ERRNO\(1\)\s*\{([^}]*)\}", text, re.DOTALL)
    assert match is not None, "no ERRNO(1) block found"
    body = match.group(1)
    tokens = []
    for line in body.splitlines():
        line = line.split("//", 1)[0].strip()
        for tok in line.split(","):
            tok = tok.strip()
            if tok and tok.isidentifier():
                tokens.append(tok)
    dupes = [t for t in tokens if tokens.count(t) > 1]
    assert len(tokens) == len(set(tokens)), f"duplicates in DENY: {dupes}"
