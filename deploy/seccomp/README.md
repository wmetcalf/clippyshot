# ClippyShot seccomp policy

This directory contains the seccomp syscall policy applied to soffice (and
its helpers) when they run inside the ClippyShot sandbox.

## File

- `clippyshot.seccomp.policy` — nsjail KAFEL DSL denylist.

## What it does

It blocks ~30 syscalls that are only used by container-escape chains and
never legitimately by soffice + its helper binaries (poppler-utils, font
config, glibc):

| Group | Syscalls |
|---|---|
| eBPF / keyring | `bpf`, `keyctl`, `add_key`, `request_key` |
| Kexec | `kexec_load`, `kexec_file_load` |
| Modules | `init_module`, `finit_module`, `delete_module` |
| Process inspect | `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp` |
| Mount / namespace | `mount`, `umount`, `umount2`, `pivot_root`, `swapon`, `swapoff`, `setns`, `unshare` |
| Host state | `reboot`, `settimeofday`, `clock_settime`, `clock_adjtime`, `sethostname`, `setdomainname` |
| x86 IO / LDT | `iopl`, `ioperm`, `modify_ldt` |
| fd-from-handle | `name_to_handle_at`, `open_by_handle_at` |
| Perf / fault / fanotify | `perf_event_open`, `userfaultfd`, `fanotify_init`, `fanotify_mark` |
| Accounting / quotas | `acct`, `quotactl` |

Denied syscalls return `EPERM` (errno 1) rather than killing the process,
so soffice sees a recoverable error instead of a hard SIGSYS. Everything
else is allowed (`DEFAULT ALLOW`).

## How it's loaded

1. The Dockerfile copies this file to `/etc/clippyshot/seccomp.policy` in
   the runtime image.
2. At sandbox construction time, `src/clippyshot/sandbox/nsjail.py` looks
   up the policy in these locations (first-match wins):
   - `/etc/clippyshot/seccomp.policy` (runtime image)
   - `<repo>/deploy/seccomp/clippyshot.seccomp.policy` (dev checkout)
3. nsjail is invoked with `--seccomp_policy <path>`. The KAFEL parser
   compiles the rules into a BPF program at startup; malformed rules
   cause nsjail to refuse to start (which is a loud-failure we want).
4. The filter is installed via `prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER)`
   immediately before the `execve()` of the child.

## Bubblewrap backend

The `BwrapSandbox` backend does not build a BPF program: bwrap expects an
already-compiled BPF blob via `--seccomp <fd>`, and building the blob at
runtime requires the `seccomp` Python bindings (shipped by the distro as
`python3-libseccomp`, not via PyPI). If the bindings are not importable,
the backend logs a WARN and continues without a per-process filter; the
nsjail backend is the preferred production backend and always has
seccomp enforced.

This is why the spec § 4.3 describes nsjail as the preferred backend and
bwrap as a fallback.

## Verifying the policy parses

Inside the Docker image:

```sh
nsjail --config /dev/null --mode o \
  --seccomp_policy /etc/clippyshot/seccomp.policy \
  -- /bin/true
```

If KAFEL cannot parse the file, nsjail prints a line number and refuses to
start. Iterate on the file until it accepts.

## Extending the denylist

Add a new line inside the `ERRNO(1) { ... }` block, alphabetized within
its group for readability. Then run `pytest tests/unit/test_sandbox_seccomp.py`
and update the `EXPECTED_DENIALS` set to match.

## Future work: tightening to an allowlist

The denylist approach is a pragmatic start — it's looser than the spec's
"deny-by-default" wording but is known to work with soffice out of the
box. Tightening to an allowlist requires:

1. Running a real conversion under strace or `seccomp-tools` with the
   current denylist and logging every syscall used.
2. Translating the resulting set into a KAFEL `ALLOW { ... }` block.
3. Flipping the `DEFAULT ALLOW` at the bottom of the file to
   `DEFAULT KILL`.
4. Running the full office-format fixture suite to catch any rarely-used
   syscalls the trace missed.
