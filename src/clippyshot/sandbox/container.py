"""Container-native sandbox backend.

Used when ClippyShot runs inside a properly-configured Docker/OCI container.
The container provides namespace isolation, dropped capabilities, no_new_privs,
read-only rootfs, non-root user, seccomp, AppArmor, and rlimits — nesting
bwrap/nsjail inside it is redundant.

Refuses to activate (a) if not inside a container and (b) if running as root.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from clippyshot.errors import SandboxError, SandboxUnavailable
from clippyshot.sandbox.base import Sandbox, SandboxRequest
from clippyshot.sandbox.bwrap import _apply_rlimits
from clippyshot.types import SandboxResult


_log = logging.getLogger("clippyshot.sandbox.container")


def _inside_container() -> bool:
    """Detect whether we're running inside a Docker/OCI container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _proc_status_map() -> dict[str, str]:
    status = Path("/proc/self/status")
    if not status.exists():
        return {}
    out: dict[str, str] = {}
    for line in status.read_text(errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _root_mount_is_read_only() -> bool:
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return False
    for line in mountinfo.read_text(errors="replace").splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 6:
            continue
        if fields[4] != "/":
            continue
        mount_opts = fields[5].split(",")
        right_parts = right.split() if right else []
        super_opts = right_parts[2].split(",") if len(right_parts) > 2 else []
        return "ro" in mount_opts or "ro" in super_opts
    return False


def _runtime_apparmor_confined() -> bool:
    current = Path("/proc/self/attr/current")
    if not current.exists():
        return False
    profile = current.read_text(errors="replace").strip().split(" ")[0]
    return bool(profile and profile != "unconfined")


def _runtime_hardening_reasons() -> list[str]:
    status = _proc_status_map()
    reasons: list[str] = []
    if status.get("NoNewPrivs") != "1":
        reasons.append("no_new_privileges_disabled")
    if status.get("Seccomp") in (None, "0"):
        reasons.append("seccomp_not_enforced")
    if status.get("CapEff", "").lower() not in ("", "0000000000000000"):
        reasons.append("capabilities_not_dropped")
    if not _root_mount_is_read_only():
        reasons.append("rootfs_not_read_only")
    if not _runtime_apparmor_confined():
        reasons.append("apparmor_unconfined")
    # The container backend cannot prove network egress is blocked from inside
    # the task. Treat that as insecure by default so callers must opt in via
    # CLIPPYSHOT_WARN_ON_INSECURE if they really want this backend.
    reasons.append("network_egress_not_verified")

    if reasons:
        _log.warning(
            "Some container hardening features are missing or could not be "
            "verified: %s. To proceed anyway, set CLIPPYSHOT_WARN_ON_INSECURE=1.",
            ", ".join(reasons),
            extra={"missing_features": reasons},
        )
    return reasons


class ContainerSandbox:
    """Run commands directly, trusting the enclosing container for isolation."""

    name = "container"

    # ContainerSandbox relies on the enclosing container's seccomp policy
    # (docker-default on Docker, RuntimeDefault on Kubernetes). That policy
    # IS being enforced — it's just inherited from the runtime, not applied
    # by ClippyShot via bwrap/nsjail. We report that honestly.
    seccomp_active = True
    seccomp_source = "container-runtime"

    def __init__(self) -> None:
        if not _inside_container():
            raise SandboxUnavailable(
                "not running inside a container; refusing to activate "
                "ContainerSandbox on a bare host"
            )
        if os.geteuid() == 0:
            raise SandboxUnavailable(
                "refusing to activate ContainerSandbox as root (uid 0); "
                "the container should run as an unprivileged user"
            )
        self._insecurity_reasons = _runtime_hardening_reasons()
        _log.info(
            "container sandbox active — relying on container runtime for "
            "namespace/capability/filesystem isolation"
        )

    def smoketest(self) -> SandboxResult:
        return self.run(SandboxRequest(argv=["/bin/true"]))

    @property
    def secure(self) -> bool:
        return not self._insecurity_reasons

    @property
    def insecurity_reasons(self) -> list[str]:
        return list(self._insecurity_reasons)

    def run(self, request: SandboxRequest) -> SandboxResult:
        # Translate sandbox paths in argv / workdir / env to host paths.
        path_map: dict[str, str] = {}
        for m in list(request.ro_mounts) + list(request.rw_mounts):
            path_map[str(m.sandbox_path)] = str(m.host_path)

        argv = [_translate_path(a, path_map) for a in request.argv]
        workdir_str = _translate_path(str(request.workdir), path_map)
        env = {k: _translate_path(v, path_map) for k, v in request.env.items()}
        if "PATH" not in env:
            env["PATH"] = "/usr/bin:/bin"

        cwd: str | None = workdir_str if Path(workdir_str).exists() else None

        start = time.monotonic()
        killed = False
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                cwd=cwd,
                env=env,
                preexec_fn=_apply_rlimits(request.limits),
                start_new_session=True,  # Isolate in a new process group for reliable cleanup
            )
        except FileNotFoundError as e:
            raise SandboxError(f"failed to start {argv[0]}: {e}") from e

        try:
            stdout, stderr = proc.communicate(timeout=request.limits.timeout_s)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            killed = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            
            try:
                stdout, stderr = proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
            exit_code = -signal.SIGKILL

        duration_ms = int((time.monotonic() - start) * 1000)
        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout or b"",
            stderr=stderr or b"",
            duration_ms=duration_ms,
            killed=killed,
        )


def _translate_path(s: str, path_map: dict[str, str]) -> str:
    """Replace sandbox paths in `s` with the corresponding host paths.

    Handles three cases (longest sandbox path wins to avoid shadowing):
    - exact match: "/sandbox/in" → "/host/input"
    - prefix match: "/sandbox/in/file.txt" → "/host/input/file.txt"
    - embedded match: "file:///sandbox/in/x" → "file:///host/input/x"

    Longest-prefix match so '/sandbox/in' doesn't shadow '/sandbox/input'.
    """
    for sandbox_path in sorted(path_map, key=len, reverse=True):
        host_path = path_map[sandbox_path]
        # Exact or prefix match (s starts with the sandbox path).
        if s == sandbox_path:
            return host_path
        if s.startswith(sandbox_path + "/"):
            return host_path + s[len(sandbox_path):]
        # Embedded match: sandbox path appears somewhere inside the string
        # (e.g. "file:///sandbox/profile" in a -env: argument).
        needle = sandbox_path + "/"
        idx = s.find(needle)
        if idx != -1:
            return s[:idx] + host_path + "/" + s[idx + len(needle):]
        if s.endswith(sandbox_path):
            return s[: -len(sandbox_path)] + host_path
    return s
