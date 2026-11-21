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
| New mount API / chroot | `fsopen`, `fsconfig`, `fsmount`, `fspick`, `move_mount`, `open_tree`, `open_tree_attr`, `mount_setattr`, `chroot`, `fchroot` (the mount-API calls and `fchroot` by number, `SYSCALL[n]`, so an older bundled kafel still compiles the policy) |
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
   cause nsjail to exit 255 ("Couldn't prepare sandboxing policy") on
   every run. The kafel bundled with nsjail 3.4 knows no syscall newer
   than about Linux 4.x, and ONE unknown name fails the whole policy, so
   newer calls are written by number: `SYSCALL[425],  // io_uring_setup`.
   The image build compiles the policy with the shipped nsjail and fails
   if it cannot.
4. The filter is installed via `prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER)`
   immediately before the `execve()` of the child.

## Bubblewrap backend

bwrap expects an already-compiled BPF blob via `--seccomp <fd>`. The
`BwrapSandbox` backend assembles it from the shared denylist
(`src/clippyshot/sandbox/seccomp_denylist.py`) with no libseccomp
dependency: the same `DEFAULT ALLOW` + `ERRNO(1)` rules as the KAFEL
policy (a parity test keeps them equal), killing any foreign-ABI (i386,
x32) syscall as libseccomp would. It used to need libseccomp's Python
bindings, a distro package the image's venv could not import, so the
image's bwrap ran with no filter at all.

## Both backends verify the filter and fail closed

Neither nsjail nor bwrap reports whether its filter took effect. Before
the first command, each backend runs a probe inside its sandbox
(`src/clippyshot/sandbox/seccomp_check.py`) that makes denylisted calls
with harmless arguments (`unshare(0)`, `open_tree`, `kcmp`,
`io_uring_setup`, `mount`). The run proceeds only if every call is EPERM
inside AND that EPERM is the sandbox's own -- at least one call is not
EPERM outside (a higher filter count is not accepted: an allow-all filter
raises it too). Otherwise
`run()`/`spawn()`/`smoketest()` raise `SeccompNotEnforced` and
auto-selection passes the backend over. `CLIPPYSHOT_WARN_ON_INSECURE`
does not waive this (gVisor tiers set it routinely for the container
backend). The in-image proof is `tests/docker/test_inner_seccomp.py`,
run by CI's `image-seccomp` job.

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
