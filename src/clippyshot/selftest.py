"""Healthcheck command."""
from __future__ import annotations

import os

import shutil
import subprocess
from pathlib import Path

from clippyshot._version import __version__
from clippyshot.detector import Detector
from clippyshot.errors import InnerSandboxLayerUnavailable, SandboxUnavailable
from clippyshot.observability import configure_logging, get_logger, set_sandbox_backend
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.sandbox.host_apparmor import (
    FIX_COMMAND,
    STOCK_BWRAP_PROFILE,
    AppArmorAttachUnavailable,
    StockBwrapProfileState,
    stock_bwrap_profile_state,
)


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


def _find_attach_probe(sandbox) -> tuple[bool, object]:
    """(has_probe_attr, probe) for the bwrap backend, seen through decorators."""
    seen = 0
    while sandbox is not None and seen < 8:
        if hasattr(sandbox, "apparmor_attach_probe"):
            return True, sandbox.apparmor_attach_probe
        sandbox = getattr(sandbox, "inner", None)
        seen += 1
    return False, None


def apparmor_attach_report(
    sandbox, *, stock: StockBwrapProfileState | None = None
) -> dict[str, str]:
    """Selftest fields for the bwrap + aa-exec attach probe and the stock bwrap profile.

    ``apparmor_attach``: ``ok`` when the selected bwrap backend's probe passed,
    ``no_aa_exec`` for a bwrap backend without aa-exec (soffice runs with no AppArmor
    profile; only selectable under CLIPPYSHOT_WARN_ON_INSECURE), ``not_applicable`` for
    other backends.
    ``stock_bwrap_profile``: the state of Ubuntu's ``bwrap-userns-restrict`` on this host,
    reported whichever backend is selected so a host upgrade that brings it back is visible
    before it breaks a bwrap deployment.
    """
    has, probe = _find_attach_probe(sandbox)
    if has and probe is not None:
        attach = "ok" if getattr(probe, "ok", False) else str(getattr(probe, "cause", "failed"))
    elif has:
        attach = "no_aa_exec"
    else:
        attach = "not_applicable"
    state = stock if stock is not None else stock_bwrap_profile_state()
    return {"apparmor_attach": attach, "stock_bwrap_profile": state.describe()}


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
        stock = stock_bwrap_profile_state()
        fields: dict[str, str] = {"stock_bwrap_profile": stock.describe()}
        if isinstance(e, AppArmorAttachUnavailable):
            fields["apparmor_attach"] = e.probe.cause
        # Only point at disabling host hardening when bwrap could be the backend at all:
        # a forced nsjail/container deployment never runs bwrap.
        forced = os.environ.get("CLIPPYSHOT_SANDBOX", "").strip().lower()
        bwrap_possible = forced == "bwrap" or (forced == "" and shutil.which("bwrap") is not None)
        if isinstance(e, InnerSandboxLayerUnavailable):
            # A nono-layer failure (static, or its smoketest under the chosen base):
            # relaxing the bwrap hardening cannot fix it.
            fields["inner_layer"] = "nono"
        elif (isinstance(e, AppArmorAttachUnavailable)
                and e.probe.cause == "stock_profile_conflict") or (
                    bwrap_possible and stock.conflict):
            fields["fix"] = FIX_COMMAND
        log.error("sandbox_unavailable", error=str(e), **fields)
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
    stock = stock_bwrap_profile_state()
    attach_report = apparmor_attach_report(sb, stock=stock)
    # Not under the `container` backend: there /etc/apparmor.d is the image's, not the host
    # policy, and bwrap never runs (gVisor / Firecracker tiers force `container`).
    if (
        stock.conflict
        and shutil.which("bwrap") is not None
        and "container" not in str(sb.name).split("+")
    ):
        # Not fatal for the selected backend, but the bwrap backend on this host cannot
        # attach clippyshot-soffice -- e.g. a host upgrade brought the stock profile back.
        log.warning(
            "stock_bwrap_profile_conflict",
            profile=STOCK_BWRAP_PROFILE,
            state=stock.describe(),
            backend=sb.name,
            fix=FIX_COMMAND,
        )
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
        # How the namespace backends proved their filter (None for `container`, whose
        # seccomp is the runtime's and is checked from /proc/self/status instead).
        seccomp_verified=getattr(getattr(sb, "seccomp_verdict", None), "detail", None),
        **attach_report,
    )
    return 0
