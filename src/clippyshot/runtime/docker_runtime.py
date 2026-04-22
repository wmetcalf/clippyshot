"""Docker runtime selection and worker container command assembly."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_log = logging.getLogger("clippyshot.runtime.docker_runtime")

_RUNSC_WARNING = "runsc unavailable; falling back to runc"
_APPARMOR_WARNING = (
    "clippyshot-soffice AppArmor profile not loaded; worker runs under docker-default"
)
_SECCOMP_WARNING = (
    "ClippyShot seccomp profile not found in image; worker runs under docker-default"
)
_DEFAULT_WORKER_UID = 10001
_DEFAULT_WORKER_GID = 10001
_DEFAULT_WORKDIR = "/job"

# 64MB was too tight — shutil.copy2(input_path, staged_input) in the
# runner puts the file into /tmp inside the worker, and a 100MB legitimate
# upload would ENOSPC. 512MB gives headroom for max_input_bytes plus
# LO's own scratch files.
_DEFAULT_TMPFS = "/tmp:rw,nosuid,noexec,size=512m"

# Worker resource caps. Conservative defaults that match a "one document,
# one minute" budget. Overridable via env vars for pathological inputs.
# 4GB because wide spreadsheet renders (SinglePageSheets on a 50-column
# sheet at 150 DPI) can produce >1GB intermediate PDFs and >2GB of RAM
# during pdftoppm rasterization. Dropping below this causes cgroup OOM-
# kills on legitimate content.
_DEFAULT_WORKER_MEMORY = "4g"
_DEFAULT_WORKER_PIDS_LIMIT = "256"
_DEFAULT_WORKER_CPUS = "1.0"
_DEFAULT_WORKER_NOFILE = "4096"

# AppArmor and seccomp profile paths inside the worker image.
_SOFFICE_APPARMOR_PROFILE = "clippyshot-soffice"
_SECCOMP_JSON_PATH = "/etc/clippyshot/seccomp.json"


def _apparmor_profile_loaded(profile_name: str) -> bool:
    """Check whether the named AppArmor profile is loaded on the host.

    Two probe paths, in order:

    1. Explicit operator assertion via the ``CLIPPYSHOT_APPARMOR_PROFILES``
       env var (comma-separated list of profile names the operator has
       loaded on the host). Use this when the dispatcher runs inside a
       container that doesn't bind /sys/kernel/security — the usual case
       with the compose stack.

    2. Direct read of ``/sys/kernel/security/apparmor/profiles``. Works
       on bare metal and in containers that bind-mount securityfs.
    """
    listed = os.environ.get("CLIPPYSHOT_APPARMOR_PROFILES", "")
    if listed:
        names = {x.strip() for x in listed.split(",") if x.strip()}
        if profile_name in names:
            return True
    try:
        with open("/sys/kernel/security/apparmor/profiles") as f:
            for line in f:
                name = line.split(" ", 1)[0].strip()
                if name == profile_name:
                    return True
    except OSError:
        return False
    return False


@dataclass(frozen=True)
class DockerRuntimeSelection:
    runtime: str
    secure: bool
    warnings: list[str] = field(default_factory=list)


def _normalize_runtimes(runtimes: Iterable[str]) -> set[str]:
    return {
        str(runtime).strip().lower() for runtime in runtimes if str(runtime).strip()
    }


def _detect_docker_runtimes() -> set[str]:
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return set()

    if proc.returncode != 0:
        return set()

    raw = proc.stdout.strip()
    if not raw:
        return set()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return set()

    if isinstance(parsed, dict):
        return _normalize_runtimes(parsed.keys())
    if isinstance(parsed, list):
        return _normalize_runtimes(parsed)
    return set()


def select_worker_runtime(
    *,
    available_runtimes: Iterable[str] | None = None,
) -> DockerRuntimeSelection:
    """Select the preferred Docker runtime for one-shot worker containers."""

    runtimes = (
        _normalize_runtimes(available_runtimes)
        if available_runtimes is not None
        else _detect_docker_runtimes()
    )
    # Operator override: CLIPPYSHOT_WORKER_RUNTIME=runc|runsc forces the
    # worker runtime regardless of auto-detection. Useful for A/B testing
    # and for operators who deliberately want runc (e.g. gVisor is installed
    # on the host but the workload requires a syscall runsc doesn't support).
    forced = os.environ.get("CLIPPYSHOT_WORKER_RUNTIME", "").strip().lower()
    if forced in ("runc", "runsc"):
        secure = forced == "runsc"
        warnings = [] if secure else [_RUNSC_WARNING]
        return DockerRuntimeSelection(runtime=forced, secure=secure, warnings=warnings)

    if "runsc" in runtimes:
        return DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])

    _log.warning(_RUNSC_WARNING, extra={"available_runtimes": sorted(runtimes)})
    return DockerRuntimeSelection(
        runtime="runc", secure=False, warnings=[_RUNSC_WARNING]
    )


def build_worker_docker_run_argv(
    *,
    image: str,
    input_path: Path,
    input_mount_path: str,
    output_dir: Path,
    output_mount_path: str,
    worker_argv: Sequence[str],
    runtime: DockerRuntimeSelection,
    container_name: str | None = None,
    worker_uid: int = _DEFAULT_WORKER_UID,
    worker_gid: int = _DEFAULT_WORKER_GID,
    workdir: str = _DEFAULT_WORKDIR,
    labels: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Build a narrow `docker run` command for a single worker job."""

    bind_input = str(Path(input_path).expanduser().resolve(strict=False))
    bind_output = str(Path(output_dir).expanduser().resolve(strict=False))

    # Resource caps. Each env var, if set, overrides the default. These
    # prevent a single pathological document from exhausting host memory,
    # PID table, or CPU cycles.
    memory = os.environ.get("CLIPPYSHOT_WORKER_MEMORY", _DEFAULT_WORKER_MEMORY)
    pids_limit = os.environ.get(
        "CLIPPYSHOT_WORKER_PIDS_LIMIT", _DEFAULT_WORKER_PIDS_LIMIT
    )
    cpus = os.environ.get("CLIPPYSHOT_WORKER_CPUS", _DEFAULT_WORKER_CPUS)
    nofile = os.environ.get("CLIPPYSHOT_WORKER_NOFILE", _DEFAULT_WORKER_NOFILE)

    # Default to SECURE ("0" = refuse to run the container sandbox unless
    # the operator explicitly opts into the insecure fallback). Operators
    # who want the nested-sandbox-less container mode must set this to "1"
    # in the dispatcher's environment; it propagates to the worker.
    warn_on_insecure = os.environ.get("CLIPPYSHOT_WARN_ON_INSECURE", "0")
    # gVisor/runsc virtualises /proc/self/status, so our ContainerSandbox
    # hardening checks (which read NoNewPrivs, Seccomp, etc. from that
    # file) can't observe docker's --security-opt flags even though they
    # ARE applied at the host level. gVisor's own syscall interposition
    # is security-equivalent to (arguably stronger than) our checks, so
    # we trust the runsc runtime and opt into the "insecure" path here.
    # Under runc the /proc reads reflect reality, so we can be strict.
    if runtime.runtime == "runsc":
        warn_on_insecure = "1"

    argv = [
        "docker",
        "run",
        "--rm",
        # NOTE: no `--init` — the image ENTRYPOINT already runs tini
        # (`ENTRYPOINT ["/usr/bin/tini", "--", "clippyshot"]` in the
        # Dockerfile), so passing --init would nest tini-in-tini and
        # emit spurious warnings on every worker startup.
        f"--runtime={runtime.runtime}",
        "--user",
        f"{worker_uid}:{worker_gid}",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--memory",
        memory,
        "--memory-swap",
        memory,  # disable swap; memory-swap == memory
        "--pids-limit",
        pids_limit,
        "--cpus",
        cpus,
        "--ulimit",
        f"nofile={nofile}:{nofile}",
        "-e",
        f"CLIPPYSHOT_WARN_ON_INSECURE={warn_on_insecure}",
        # Force the ContainerSandbox backend — nsjail/bwrap can't unshare
        # user namespaces from inside a cap-dropped container anyway, and
        # forcing here avoids three startup smoketests per worker plus the
        # silent-wrong-backend failure mode where nsjail is picked but
        # cannot attach AppArmor profiles (which aren't loaded in-container).
        "-e",
        "CLIPPYSHOT_SANDBOX=container",
        # Dedicated writable cache dir for any library that writes under
        # $HOME (we use /tmp because rootfs is readonly).
        "-e",
        "HOME=/tmp",
        "--tmpfs",
        _DEFAULT_TMPFS,
        "--mount",
        f"type=bind,src={bind_input},dst={input_mount_path},readonly",
        "--mount",
        f"type=bind,src={bind_output},dst={output_mount_path}",
        "--workdir",
        workdir,
    ]

    # Attach the clippyshot-soffice AppArmor profile IF it's loaded on the
    # host kernel. We can't load it from inside a cap-dropped container,
    # so the host operator must do that ahead of time (see
    # deploy/apparmor/README.md). If not loaded, docker-default applies
    # and we record a warning on the selection object so it surfaces in
    # the job metadata.
    if _apparmor_profile_loaded(_SOFFICE_APPARMOR_PROFILE):
        argv.extend(["--security-opt", f"apparmor={_SOFFICE_APPARMOR_PROFILE}"])
    else:
        runtime.warnings.append(_APPARMOR_WARNING)

    # Attach the ClippyShot seccomp profile. Docker reads --security-opt=
    # seccomp=<path> from the HOST filesystem, not the dispatcher's view.
    # Operators who want the tighter-than-default profile must:
    #   1. mount the project's deploy/seccomp/clippyshot.seccomp.json to a
    #      host-readable path, and
    #   2. set CLIPPYSHOT_SECCOMP_JSON_HOST to that host path.
    # We don't (can't) verify the path exists — the dispatcher lives in
    # its own filesystem; the host path only resolves in the docker daemon
    # context. If the value is wrong, `docker run` will error at launch
    # and the job will fail with a clear stderr. Without this, docker-
    # default seccomp still applies (which blocks the most dangerous
    # syscalls already — keyctl/bpf/clock_settime/etc).
    seccomp_host_path = os.environ.get("CLIPPYSHOT_SECCOMP_JSON_HOST", "").strip()
    if seccomp_host_path:
        argv.extend(["--security-opt", f"seccomp={seccomp_host_path}"])
    else:
        runtime.warnings.append(_SECCOMP_WARNING)

    # Propagate scanner config so the worker honors operator-set defaults.
    for name in (
        "CLIPPYSHOT_ENABLE_QR",
        "CLIPPYSHOT_ENABLE_OCR",
        "CLIPPYSHOT_OCR_ALL",
        "CLIPPYSHOT_OCR_LANG",
        "CLIPPYSHOT_OCR_PSM",
        "CLIPPYSHOT_OCR_TIMEOUT_S",
        "CLIPPYSHOT_QR_FORMATS",
        "CLIPPYSHOT_ZXING_TIMEOUT_S",
        "CLIPPYSHOT_ZIP_PASSWORD",
    ):
        v = os.environ.get(name)
        if v is not None:
            argv.extend(["-e", f"{name}={v}"])

    for name, value in (extra_env or {}).items():
        argv.extend(["-e", f"{name}={value}"])

    if container_name:
        argv.extend(["--name", container_name])
    for key, value in (labels or {}).items():
        argv.extend(["--label", f"{key}={value}"])
    argv.append(image)
    argv.extend(worker_argv)
    return argv
