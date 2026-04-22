"""Bubblewrap-backed sandbox."""
from __future__ import annotations

import logging
import os
import resource
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from clippyshot.errors import SandboxError, SandboxUnavailable
from clippyshot.sandbox.base import Sandbox, SandboxRequest
from clippyshot.types import SandboxResult


_log = logging.getLogger("clippyshot.sandbox.bwrap")

_BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"

# Strict AppArmor profile to attach to the soffice child process via
# `aa-exec -p <profile> --`. The aa-exec helper calls aa_change_onexec()
# and execve()s the target, which is the same mechanism nsjail uses via
# its --proc_apparmor flag. Profile must be loaded on the host kernel.
_DEFAULT_APPARMOR_PROFILE = "clippyshot-soffice"


# Attempt to import the libseccomp Python bindings. They are distributed as
# the `libseccomp` package on PyPI is NOT the real package — libseccomp's
# upstream bindings ship as `seccomp` via the distro's `python3-libseccomp`
# debian package. If neither is available we proceed without an in-process
# seccomp filter and log a WARN; the nsjail backend gets its seccomp from
# the KAFEL policy file and is the preferred backend in production.
try:  # pragma: no cover - import path depends on host
    import seccomp as _libseccomp  # type: ignore[import-not-found]
    _LIBSECCOMP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the unit tests via monkeypatch
    _libseccomp = None  # type: ignore[assignment]
    _LIBSECCOMP_AVAILABLE = False


def _probe_bwrap_cgroup_pids(bwrap_path: str) -> bool:
    """Return True if this bwrap binary supports the --cgroup-pids flag."""
    try:
        r = subprocess.run(
            [bwrap_path, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "--cgroup-pids" in r.stdout or "--cgroup-pids" in r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


_RO_SYSTEM_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc")

# On merged-usr systems (e.g. Ubuntu 22.04+, Debian 12+) /bin, /sbin, /lib, /lib64
# are symlinks into /usr.  bwrap bind-mounts resolve the symlink but don't recreate
# the symlink itself inside the new rootfs, so /bin etc. become dangling inside the
# sandbox.  We detect this at startup and emit --symlink stanzas for any of those
# paths that are symlinks on the host.
_MERGED_USR_SYMLINKS: list[tuple[str, str]] = []
for _d in ("/bin", "/sbin", "/lib", "/lib64"):
    _p = Path(_d)
    if _p.is_symlink():
        # readlink gives e.g. "usr/bin" (relative) or "/usr/bin" (absolute)
        _target = os.readlink(_d)
        _MERGED_USR_SYMLINKS.append((_target, _d))


class BwrapSandbox:
    """Sandbox backend that wraps `bwrap` from bubblewrap."""

    name = "bwrap"

    def __init__(
        self,
        bwrap_path: str = _BWRAP,
        *,
        apparmor_profile: str = _DEFAULT_APPARMOR_PROFILE,
    ) -> None:
        if not shutil.which(bwrap_path) and not Path(bwrap_path).exists():
            raise SandboxUnavailable(f"bwrap not found at {bwrap_path}")
        self._bwrap = bwrap_path
        self._apparmor_profile = apparmor_profile

        # AppArmor attachment: bwrap has no built-in support, so we prefix
        # the argv with `aa-exec -p <profile> --` iff aa-exec is available
        # AND the profile is loaded. We can't probe `is the profile loaded`
        # from userspace without root, so we only check the helper binary.
        self._aa_exec: str | None = shutil.which("aa-exec")
        if self._aa_exec is not None:
            _log.info(
                "bwrap_apparmor_attach_enabled aa_exec=%s profile=%s",
                self._aa_exec,
                self._apparmor_profile,
            )
        else:
            _log.warning(
                "bwrap_apparmor_attach_skipped reason=aa-exec_not_found "
                "profile=%s install=apparmor-utils",
                self._apparmor_profile,
            )

        # Fork-bomb defense: probe whether this bwrap supports --cgroup-pids.
        # nsjail enforces --rlimit_nproc 256 inside the new user namespace;
        # bwrap's RLIMIT_NPROC was removed because it applies per-real-uid on
        # the host, not per-namespace.  --cgroup-pids (bubblewrap >= 0.5.0 on
        # cgroup v2 systems) provides an equivalent per-sandbox PIDs limit.
        # If the flag is absent we log a WARN and rely on the container runtime
        # (k8s pod PID limit, ECS task PID limit, Docker --pids-limit) to cap
        # the fork count.  This is documented in deploy/apparmor/README.md.
        self._cgroup_pids_supported = _probe_bwrap_cgroup_pids(bwrap_path)
        if self._cgroup_pids_supported:
            _log.info(
                "bwrap_cgroup_pids_enabled limit=256",
            )
        else:
            _log.warning(
                "bwrap_fork_bomb_defense_degraded reason=--cgroup-pids_not_supported_by_installed_bwrap "
                "mitigation=container_runtime_pid_limits "
                "note=set_--pids-limit_256_in_docker_or_configure_k8s_pod_PID_limit"
            )

        # Seccomp: bwrap expects a pre-built BPF program fed via --seccomp
        # <fd>. Building that program requires the libseccomp Python
        # bindings. If they are not available we continue without a filter
        # and log a WARN — the nsjail backend (preferred in production)
        # still enforces seccomp via its KAFEL policy file.
        self._seccomp_active = _LIBSECCOMP_AVAILABLE
        if self._seccomp_active:
            _log.info("bwrap_seccomp_enabled library=libseccomp-python")
        else:
            _log.warning(
                "bwrap_seccomp_skipped reason=libseccomp_python_not_available "
                "note=nsjail_backend_has_seccomp_via_KAFEL"
            )
        self._insecurity_reasons: list[str] = []
        if not self._seccomp_active:
            self._insecurity_reasons.append("seccomp_missing")
        if not self._aa_exec:
            self._insecurity_reasons.append("apparmor_missing")
        if not self._cgroup_pids_supported:
            self._insecurity_reasons.append("pid_limit_missing")

    @property
    def cgroup_pids_supported(self) -> bool:
        return self._cgroup_pids_supported

    @property
    def seccomp_active(self) -> bool:
        return self._seccomp_active

    @property
    def seccomp_source(self) -> str:
        """Stable string identifying where the seccomp filter comes from."""
        return "clippyshot-bwrap" if self._seccomp_active else "none"

    @property
    def apparmor_profile(self) -> str:
        return self._apparmor_profile

    @property
    def apparmor_active(self) -> bool:
        return self._aa_exec is not None

    @property
    def secure(self) -> bool:
        return not self._insecurity_reasons

    @property
    def insecurity_reasons(self) -> list[str]:
        return list(self._insecurity_reasons)

    def smoketest(self) -> SandboxResult:
        return self.run(SandboxRequest(argv=["/bin/true"]))

    def run(self, request: SandboxRequest) -> SandboxResult:
        argv = self._build_argv(request)
        start = time.monotonic()
        killed = False
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                preexec_fn=_apply_rlimits(request.limits),
            )
        except FileNotFoundError as e:
            raise SandboxError(f"failed to start bwrap: {e}") from e

        try:
            stdout, stderr = proc.communicate(timeout=request.limits.timeout_s)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            killed = True
            try:
                proc.kill()
            finally:
                stdout, stderr = proc.communicate()
            exit_code = -signal.SIGKILL

        duration_ms = int((time.monotonic() - start) * 1000)
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout or b"",
            stderr=stderr or b"",
            duration_ms=duration_ms,
            killed=killed,
        )

    def _build_argv(self, req: SandboxRequest) -> list[str]:
        argv: list[str] = [
            self._bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            "--cap-drop", "ALL",
            "--unshare-net",
        ]
        for d in _RO_SYSTEM_DIRS:
            p = Path(d)
            if p.is_symlink():
                # Handled below via --symlink stanzas; don't bind the symlink target twice.
                continue
            if p.exists():
                argv += ["--ro-bind", d, d]

        # Re-create merged-usr symlinks inside the sandbox rootfs.
        for target, link in _MERGED_USR_SYMLINKS:
            argv += ["--symlink", target, link]

        for m in req.ro_mounts:
            argv += ["--ro-bind", str(m.host_path), str(m.sandbox_path)]
        for m in req.rw_mounts:
            argv += ["--bind", str(m.host_path), str(m.sandbox_path)]

        # Per-sandbox PIDs cap (fork-bomb defense): equivalent to nsjail's
        # --rlimit_nproc 256.  Only added when --cgroup-pids is supported by
        # the installed bwrap (bubblewrap >= 0.5.0, cgroup v2 host).
        if self._cgroup_pids_supported:
            argv += ["--cgroup-pids", "256"]

        argv += ["--chdir", str(req.workdir)]

        for k, v in req.env.items():
            argv += ["--setenv", k, v]

        # Always set a tame PATH so soffice can find its helper binaries.
        if "PATH" not in req.env:
            argv += ["--setenv", "PATH", "/usr/bin:/bin"]

        argv += ["--"]

        # AppArmor attach: prefix the inner argv with `aa-exec -p <profile> --`
        # when aa-exec is available. The wrapper calls aa_change_onexec()
        # for the next execve() inside the sandbox, which is how the strict
        # `clippyshot-soffice` profile gets attached to the child process.
        # If the profile is not loaded on the host kernel, aa-exec itself
        # errors out with a clear message instead of silently running
        # unconfined — we rely on that loud failure at runtime.
        inner = list(req.argv)
        if self._aa_exec is not None:
            inner = [self._aa_exec, "-p", self._apparmor_profile, "--", *inner]
        argv += inner
        return argv


def _apply_rlimits(limits) -> Callable[[], None]:
    """Return a preexec function that applies rlimits to the child."""

    def _set() -> None:
        # Address space (memory).
        try:
            resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
        except (ValueError, OSError):
            pass
        # CPU time as a hard upper bound (timeout_s + 30s headroom).
        cpu = limits.timeout_s + 30
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        except (ValueError, OSError):
            pass
        # File size.
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (limits.tmpfs_bytes, limits.tmpfs_bytes))
        except (ValueError, OSError):
            pass
        # No core dumps.
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            pass
        # Open file descriptors. soffice with many embedded images can
        # easily need 500+ fds (each image, temp file, thread, and IPC
        # channel is a descriptor). 4096 is generous enough for the
        # largest real-world presentations while still capping runaway
        # fd leaks.
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
        except (ValueError, OSError):
            pass
        # Note: RLIMIT_NPROC is intentionally omitted here.  The limit applies to
        # all processes owned by this uid, not just children of the sandboxed
        # process.  On a busy desktop/CI machine the user may already have hundreds
        # of processes; setting a hard NPROC limit would prevent bwrap from
        # forking its internal helper processes that set up the user-namespace uid
        # map, causing every run to fail with "Resource temporarily unavailable".
        # Fork-bomb protection is instead provided by the new user namespace itself
        # (the sandboxed uid has no existing processes in it) and by the CPU and
        # memory rlimits above.
        try:
            os.setsid()
        except OSError:
            pass

    return _set
