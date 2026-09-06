"""nsjail-backed sandbox."""
from __future__ import annotations

import logging
import shutil
import signal
import subprocess
import time
from pathlib import Path

from clippyshot.errors import SandboxError, SandboxUnavailable
from clippyshot.sandbox.base import SandboxRequest
from clippyshot.types import SandboxResult


_log = logging.getLogger("clippyshot.sandbox.nsjail")

_NSJAIL = shutil.which("nsjail") or "/usr/local/bin/nsjail"

# Seccomp policy file (nsjail KAFEL DSL). We look for the file in a few
# canonical locations so the same code works both in the Docker image (where
# it's copied to /etc/clippyshot/seccomp.policy by the Dockerfile) and in a
# dev checkout (where it lives at deploy/seccomp/clippyshot.seccomp.policy
# relative to the repo root).
_SECCOMP_POLICY_CANDIDATES = (
    Path("/etc/clippyshot/seccomp.policy"),
    Path(__file__).resolve().parents[3] / "deploy" / "seccomp" / "clippyshot.seccomp.policy",
)


def _probe_nsjail_proc_apparmor(nsjail_path: str) -> bool:
    try:
        r = subprocess.run(
            [nsjail_path, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return "--proc_apparmor" in r.stdout or "--proc_apparmor" in r.stderr


def _find_seccomp_policy() -> Path | None:
    for candidate in _SECCOMP_POLICY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


# Default strict AppArmor profile for the soffice process.  The profile
# lives at deploy/apparmor/clippyshot-soffice; it's attached via
# nsjail --proc_apparmor= which is the in-kernel AA_CHANGE_ONEXEC path.
_DEFAULT_APPARMOR_PROFILE = "clippyshot-soffice"


# Read-only system directories. On merged-usr systems (Ubuntu 24.04), /bin,
# /sbin, /lib, /lib64 are symlinks to /usr/bin etc., and we have to recreate
# the symlinks inside the jail rather than bind-mount the targets.
_USR_DIRS = ("/usr", "/etc")
_USR_SYMLINKS = {
    "/bin": "usr/bin",
    "/sbin": "usr/sbin",
    "/lib": "usr/lib",
    "/lib64": "usr/lib64",
}


def _is_deadline_kill(
    exit_code: int, stderr: bytes, *, elapsed_s: float, timeout_s: float
) -> bool:
    """Whether nsjail stopped the child for exceeding its deadline.

    `killed` is read as "timed out" -- `LibreOfficeRunner` treats it that way
    and the UI renders it as a timeout -- so this must not fire for every death
    by signal. Three kinds of evidence, and they are not equal:

    * 109, and the stderr markers, are nsjail SAYING it enforced the limit.
      Unambiguous, so they stand alone.
    * 137 / 143 are only "died by SIGKILL / SIGTERM", which an OOM kill or a
      child killing itself produces just as well. Reporting those as timeouts
      would misdiagnose memory exhaustion as a deadline, so they count only
      when the run actually REACHED the deadline.

    Other signal deaths never count: a SIGSEGV (139) is the command crashing,
    not the sandbox stopping it. The version spread is real -- the nsjail in
    the worker image returns 137 where this code once assumed only 109.
    """
    if exit_code == 109:
        return True
    if exit_code != 0 and (
        b"time >=" in stderr or b"timed out" in stderr.lower()
    ):
        return True
    if exit_code in (128 + signal.SIGKILL, 128 + signal.SIGTERM):
        # A small tolerance: the deadline fires at the limit, and the measured
        # elapsed time straddles it (1007 ms against a 1 s limit, measured).
        return elapsed_s + 0.05 >= timeout_s
    return False


class NsjailSandbox:
    name = "nsjail"

    def __init__(
        self,
        nsjail_path: str = _NSJAIL,
        *,
        apparmor_profile: str = _DEFAULT_APPARMOR_PROFILE,
        seccomp_policy: Path | None = None,
    ) -> None:
        if not shutil.which(nsjail_path) and not Path(nsjail_path).exists():
            raise SandboxUnavailable(f"nsjail not found at {nsjail_path}")
        self._nsjail = nsjail_path
        self._apparmor_profile = apparmor_profile
        # Resolve the seccomp policy path at construction time so we can log
        # once and the argv builder never has to check disk again.
        self._seccomp_policy: Path | None = (
            seccomp_policy if seccomp_policy is not None else _find_seccomp_policy()
        )
        self._proc_apparmor_supported = _probe_nsjail_proc_apparmor(self._nsjail)
        if self._seccomp_policy is None:
            _log.warning(
                "nsjail_seccomp_policy_missing searched=%s",
                [str(p) for p in _SECCOMP_POLICY_CANDIDATES],
            )
        else:
            _log.info(
                "nsjail_seccomp_policy_active path=%s apparmor=%s",
                str(self._seccomp_policy),
                self._apparmor_profile,
            )
        self._insecurity_reasons: list[str] = []
        if self._seccomp_policy is None:
            self._insecurity_reasons.append("seccomp_policy_missing")
        if not self._proc_apparmor_supported:
            _log.warning(
                "nsjail_proc_apparmor_skipped reason=unsupported_by_installed_nsjail "
                "profile=%s",
                self._apparmor_profile,
            )

    @property
    def seccomp_active(self) -> bool:
        return self._seccomp_policy is not None

    @property
    def seccomp_source(self) -> str:
        """Stable string identifying where the seccomp filter comes from."""
        return "clippyshot-nsjail" if self._seccomp_policy is not None else "none"

    @property
    def apparmor_profile(self) -> str:
        return self._apparmor_profile

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
            )
        except FileNotFoundError as e:
            raise SandboxError(f"failed to start nsjail: {e}") from e

        try:
            # Give nsjail a few extra seconds beyond its --time_limit to clean up.
            stdout, stderr = proc.communicate(timeout=request.limits.timeout_s + 5)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            killed = True
            try:
                proc.kill()
            finally:
                stdout, stderr = proc.communicate()
            exit_code = -signal.SIGKILL

        # How nsjail reports killing the child on --time_limit, which is
        # version-dependent and NOT only 109:
        #
        #   * 109 -- the older "signal + 100" convention this used to assume;
        #   * 137 / 143 -- the shell's 128+signal for SIGKILL / SIGTERM, which
        #     is what the nsjail in the worker image actually returns (measured:
        #     exit 137 on a 1 s --time_limit against `sleep 10`);
        #   * a stderr marker, when the run is not --quiet.
        #
        # Recognising only 109 reported a timeout as an ordinary non-zero exit,
        # so a caller distinguishing "the sandbox stopped it" from "the command
        # failed" got the wrong answer -- and with --quiet there is no stderr to
        # fall back on. Signal deaths other than KILL/TERM stay unkilled: a
        # SIGSEGV (139) is the command crashing, not the sandbox stopping it.
        if not killed:
            killed = _is_deadline_kill(
                exit_code, stderr or b"",
                elapsed_s=time.monotonic() - start,
                timeout_s=float(request.limits.timeout_s),
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout or b"",
            stderr=stderr or b"",
            duration_ms=duration_ms,
            killed=killed,
        )

    def spawn(self, request: SandboxRequest, **popen_kwargs) -> subprocess.Popen:
        """Start a LONG-LIVED process in the jail; the caller owns its pipes.

        No `--time_limit`: a warm helper is meant to outlive any single request, and
        `Limits.timeout_s` caps at 600s, so reusing the one-shot argv would kill the
        helper ten minutes in. Each REQUEST is still bounded by the client, which
        SIGKILLs a hung helper -- and killing this process kills the jailed one
        (verified on nsjail and bwrap: zero survivors).
        """
        return subprocess.Popen(self._build_argv(request, persistent=True), **popen_kwargs)

    def _build_argv(self, req: SandboxRequest, *, persistent: bool = False) -> list[str]:
        argv: list[str] = [
            self._nsjail,
            "--mode", "o",  # one-shot
            "--quiet",
            "--really_quiet",
            # NOTE: we used to pass --disable_proc here but LibreOffice
            # refuses to start without /proc ("ERROR: /proc not mounted").
            # nsjail's default mount gives us a pid-namespace-scoped
            # procfs — it only shows processes inside the sandbox, so
            # there's no host info leak.
            "--iface_no_lo",
            "--rlimit_as", str(req.limits.memory_bytes // (1024 * 1024)),
            "--rlimit_fsize", str(req.limits.tmpfs_bytes // (1024 * 1024)),
            "--rlimit_nofile", "4096",
            "--rlimit_nproc", "256",
            "--rlimit_core", "0",
            "--cwd", str(req.workdir),
            "--user", "65534",
            "--group", "65534",
            "--hostname", "clippy",
        ]
        if not persistent:
            # A one-shot run gets the request's deadline. A persistent helper does not:
            # see spawn().
            argv += ["--time_limit", str(req.limits.timeout_s)]

        # Read-only system bind mounts.  Real directories use --bindmount_ro;
        # merged-usr symlinks are recreated inside the jail with --symlink.
        for d in _USR_DIRS:
            if Path(d).exists():
                argv += ["--bindmount_ro", f"{d}:{d}"]
        for link, target in _USR_SYMLINKS.items():
            if Path(link).is_symlink():
                argv += ["--symlink", f"{target}:{link}"]
            elif Path(link).exists():
                # Real directory (non-merged-usr distro): bind it normally.
                argv += ["--bindmount_ro", f"{link}:{link}"]

        # Tmpfs for /tmp.
        argv += ["--tmpfsmount", "/tmp"]

        # nsjail does not populate /dev automatically; soffice's shell
        # wrapper needs /dev/null (at minimum) and LO libraries use
        # /dev/{zero,random,urandom} for mmap seeding and RNG.
        for dev in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
            if Path(dev).exists():
                argv += ["--bindmount_ro", f"{dev}:{dev}"]

        for m in req.ro_mounts:
            argv += ["--bindmount_ro", f"{m.host_path}:{m.sandbox_path}"]
        for m in req.rw_mounts:
            argv += ["--bindmount", f"{m.host_path}:{m.sandbox_path}"]

        for k, v in req.env.items():
            argv += ["--env", f"{k}={v}"]
        if "PATH" not in req.env:
            argv += ["--env", "PATH=/usr/bin:/bin"]

        # Seccomp policy (KAFEL DSL denylist of dangerous syscalls). Attached
        # only if the policy file is present on disk; otherwise we already
        # logged a WARN at construction time.
        if self._seccomp_policy is not None:
            argv += ["--seccomp_policy", str(self._seccomp_policy)]

        # Strict AppArmor profile for the child process. nsjail calls
        # aa_change_onexec() with this name before execve(), so the profile
        # must already be loaded on the host kernel (see deploy/apparmor/).
        if self._proc_apparmor_supported and req.attach_apparmor:
            argv += ["--proc_apparmor", self._apparmor_profile]

        argv += ["--", *req.argv]
        return argv
