"""Healthcheck command."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from clippyshot._version import __version__
from clippyshot.detector import Detector
from clippyshot.errors import SandboxUnavailable
from clippyshot.observability import configure_logging, get_logger, set_sandbox_backend
from clippyshot.sandbox.detect import select_sandbox


def detect_runtime_apparmor_profile() -> str:
    """Return the AppArmor profile attached to the *current* (runtime) process.

    This is what /proc/self/attr/current reports — typically
    `clippyshot-runtime` when the runtime AA profile is loaded, or
    `unconfined` otherwise. It is NOT the profile soffice runs under;
    see `detect_soffice_apparmor_profile` for that.
    """
    p = Path("/proc/self/attr/current")
    if not p.exists():
        return "unconfined"
    raw = p.read_text(errors="replace").strip().split(" ")[0]
    return raw or "unconfined"


def detect_soffice_apparmor_profile(sandbox_backend=None) -> str:
    """Return the AppArmor profile configured for the sandboxed soffice child.

    This is the profile name that the sandbox backend will attach to
    soffice via `nsjail --proc_apparmor` or `aa-exec -p <profile> --`.
    It's the *configured* name, not a runtime measurement — if the profile
    is not loaded on the host kernel, soffice will fail to start at runtime
    (which is the desired loud-failure behavior).

    When `sandbox_backend` is None we return the default profile name that
    NsjailSandbox/BwrapSandbox would use.
    """
    if sandbox_backend is not None and hasattr(sandbox_backend, "apparmor_profile"):
        return sandbox_backend.apparmor_profile
    return "clippyshot-soffice"


# Back-compat alias: the old single-profile field was misleading because it
# always returned the runtime's profile, never the soffice child's. New code
# should call `detect_runtime_apparmor_profile` or
# `detect_soffice_apparmor_profile` explicitly.
detect_apparmor_profile = detect_runtime_apparmor_profile


def run_selftest() -> int:
    configure_logging()
    log = get_logger("clippyshot.selftest")
    log.info("selftest_started", version=__version__)

    # 1. Detector loads (Magika model loads).
    try:
        Detector()
    except Exception as e:  # noqa: BLE001
        log.error("detector_load_failed", error=str(e))
        return 1

    # 2. Sandbox backend selects + smoketests.
    try:
        sb = select_sandbox()
        smoke = sb.smoketest()
    except SandboxUnavailable as e:
        log.error("sandbox_unavailable", error=str(e))
        return 1
    except Exception as e:  # noqa: BLE001
        log.error("sandbox_smoketest_failed", error=str(e))
        return 1
    if smoke.exit_code != 0 or smoke.killed:
        log.error(
            "sandbox_smoketest_nonzero",
            backend=sb.name,
            exit_code=smoke.exit_code,
            killed=smoke.killed,
            stderr=smoke.stderr.decode(errors="replace")[:500],
        )
        return 1
    set_sandbox_backend(sb.name)
    secure = bool(getattr(sb, "secure", False))
    insecurity_reasons = list(getattr(sb, "insecurity_reasons", []))

    # 3. soffice present.
    if shutil.which("soffice") is None:
        log.error("soffice_missing")
        return 1
    try:
        soffice_version = subprocess.run(
            ["soffice", "--version"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except Exception as e:  # noqa: BLE001
        log.error("soffice_version_failed", error=str(e))
        return 1

    # 4. pdftoppm present.
    if shutil.which("pdftoppm") is None:
        log.error("pdftoppm_missing")
        return 1

    log.info(
        "selftest_passed",
        sandbox=sb.name,
        secure=secure,
        insecurity_reasons=insecurity_reasons,
        soffice=soffice_version,
        runtime_apparmor=detect_runtime_apparmor_profile(),
        soffice_apparmor=detect_soffice_apparmor_profile(sb),
        seccomp=getattr(sb, "seccomp_source", "none"),
    )
    return 0
