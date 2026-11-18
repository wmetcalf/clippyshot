"""Host AppArmor state that decides whether bwrap + aa-exec can confine soffice.

Ubuntu's stock ``bwrap-userns-restrict`` profile (apparmor 4.1+, now on the hosted CI
runner images and newer Ubuntu releases) attaches to ``/usr/bin/bwrap`` as ``bwrap`` and
stacks every child bwrap starts into ``unpriv_bwrap``, which may not change profile. So
``aa-exec -p clippyshot-soffice`` inside bwrap fails with "insufficient permissions to
change to the profile", and loading ``clippyshot-bwrap`` next to it (both attach
``/usr/bin/bwrap``) breaks bwrap's uid map ("setting up uid map: Permission denied").
ClippyShot fails closed either way -- it never runs soffice unconfined -- but the failure
used to surface only as "bwrap smoketest exit=1".

This module turns that into a named diagnosis:

* :func:`stock_bwrap_profile_state` -- is the stock profile installed, disabled, loaded?
  Reads files only (``/etc/apparmor.d`` and the AppArmor securityfs, whose per-profile
  ``name`` files are world-readable), so it needs no root.
* :func:`probe_aa_exec` -- the same probe CI runs (sandboxed bwrap, then ``aa-exec -p
  clippyshot-soffice -- /bin/true``) with the failure classified.

``clippyshot setup-sandbox --disable-stock-bwrap-profile`` is the fix it points at.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from clippyshot.errors import SandboxUnavailable

STOCK_BWRAP_PROFILE = "bwrap-userns-restrict"
# The profile names that file loads (what `aa-status` lists).
STOCK_BWRAP_PROFILE_NAMES = frozenset({"bwrap", "unpriv_bwrap"})
APPARMOR_D = Path("/etc/apparmor.d")
SECURITYFS = Path("/sys/kernel/security/apparmor")
FIX_COMMAND = "clippyshot setup-sandbox --disable-stock-bwrap-profile"
LOAD_COMMAND = "clippyshot setup-sandbox --apply"
README = "deploy/apparmor/README.md"

# stderr signatures. The first is aa-exec refused by unpriv_bwrap; the second is bwrap
# itself refused when clippyshot-bwrap and the stock `bwrap` profile are both loaded.
_STOCK_SIGNATURES = (
    "insufficient permissions to change to the profile",
    "setting up uid map",
)
_NOT_LOADED_SIGNATURE = "does not exist"

Runner = Callable[..., Any]


def loaded_profile_names(securityfs: Path = SECURITYFS) -> frozenset[str] | None:
    """Names of the loaded AppArmor profiles, or None when they cannot be read.

    ``policy/profiles/<dir>/name`` is readable without privilege; the flat ``profiles``
    file (``<name> (<mode>)`` per line) is root-only on Ubuntu and is the fallback.
    """
    tree = securityfs / "policy" / "profiles"
    try:
        names = {
            (d / "name").read_text(errors="replace").strip()
            for d in tree.iterdir()
            if (d / "name").is_file()
        }
        return frozenset(n for n in names if n)
    except OSError:
        pass
    try:
        lines = (securityfs / "profiles").read_text(errors="replace").splitlines()
    except OSError:
        return None
    return frozenset(
        line.rsplit(" (", 1)[0].strip() for line in lines if line.strip()
    )


@dataclass(frozen=True)
class StockBwrapProfileState:
    installed: bool          # /etc/apparmor.d/bwrap-userns-restrict exists
    disabled: bool           # /etc/apparmor.d/disable/bwrap-userns-restrict exists
    loaded: bool | None      # bwrap/unpriv_bwrap in the kernel; None = could not tell

    @property
    def conflict(self) -> bool:
        """Does it (or will it, after the next boot / apparmor reload) block aa-exec?"""
        return self.loaded is True or (self.installed and not self.disabled)

    def describe(self) -> str:
        if not self.installed and self.loaded is not True:
            return "absent"
        if self.loaded is True:
            where = "installed" if self.installed else "not in /etc/apparmor.d"
            extra = ", disabled on disk but still" if self.disabled else ","
            return f"loaded ({where}{extra} in the kernel)"
        if not self.disabled:
            if self.loaded is False:
                return "installed and enabled (not loaded now; returns on reboot/apparmor reload)"
            return "installed and enabled (load state unknown)"
        return "disabled"


def stock_bwrap_profile_state(
    *, apparmor_d: Path = APPARMOR_D, securityfs: Path = SECURITYFS
) -> StockBwrapProfileState:
    names = loaded_profile_names(securityfs)
    return StockBwrapProfileState(
        installed=(apparmor_d / STOCK_BWRAP_PROFILE).exists(),
        disabled=(apparmor_d / "disable" / STOCK_BWRAP_PROFILE).exists()
        or (apparmor_d / "disable" / STOCK_BWRAP_PROFILE).is_symlink(),
        loaded=None if names is None else bool(names & STOCK_BWRAP_PROFILE_NAMES),
    )


def aa_exec_probe_argv(bwrap: str, aa_exec: str, profile: str) -> list[str]:
    """The probe CI runs (`.github/workflows/ci.yml`), argv for argv.

    The fresh ``--proc /proc`` matters: aa-exec writes /proc/self/attr/exec, and the
    host's /proc inherited through ``--ro-bind / /`` is read-only on some libapparmor
    versions (Ubuntu 24.04: "Read-only file system"), which would fail the probe for a
    reason unrelated to AppArmor policy. ``BwrapSandbox._build_argv`` mounts it too.
    """
    return [
        bwrap, "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/",
        "--proc", "/proc", "--",
        aa_exec, "-p", profile, "--", "/bin/true",
    ]


@dataclass(frozen=True)
class AaExecProbe:
    ok: bool
    # ok | stock_profile_conflict | profile_not_loaded | failed
    cause: str
    profile: str
    argv: tuple[str, ...]
    exit_code: int | None
    stderr: str
    stock: StockBwrapProfileState

    def message(self) -> str:
        if self.ok:
            return f"bwrap + aa-exec attached AppArmor profile {self.profile!r}"
        head = (
            f"bwrap cannot attach AppArmor profile {self.profile!r} to its sandboxed child "
            f"(probe: {' '.join(self.argv)} -> exit {self.exit_code}: {self.stderr or 'no stderr'})"
        )
        if self.cause == "stock_profile_conflict":
            return (
                f"{head}. Ubuntu's stock AppArmor profile {STOCK_BWRAP_PROFILE!r} "
                f"(state: {self.stock.describe()}) attaches to bwrap and confines its "
                "children to 'unpriv_bwrap', which may not change profile; ClippyShot "
                "refuses to run soffice unconfined. Fix: run "
                f"`{FIX_COMMAND}` (disables {STOCK_BWRAP_PROFILE} persistently and loads "
                f"the ClippyShot profiles; see {README})."
            )
        if self.cause == "profile_not_loaded":
            return (
                f"{head}. The {self.profile!r} profile is not loaded on this host kernel. "
                f"Fix: run `{LOAD_COMMAND}` (see {README})."
            )
        tail = ""
        if self.stock.conflict:
            tail = (
                f" The stock {STOCK_BWRAP_PROFILE!r} profile is also present "
                f"({self.stock.describe()}); `{FIX_COMMAND}` disables it."
            )
        return f"{head}.{tail} See {README}."


def probe_aa_exec(
    bwrap: str,
    aa_exec: str,
    profile: str,
    *,
    runner: Runner = subprocess.run,
    stock_state: StockBwrapProfileState | None = None,
    timeout_s: float = 15,
) -> AaExecProbe:
    """Run the attach probe once and classify a failure."""
    argv = aa_exec_probe_argv(bwrap, aa_exec, profile)
    exit_code: int | None
    try:
        r = runner(argv, capture_output=True, timeout=timeout_s)
        exit_code = r.returncode
        raw = r.stderr or b""
        stderr = (raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)).strip()
    except (OSError, subprocess.SubprocessError) as e:
        exit_code, stderr = None, f"{type(e).__name__}: {e}"
    stderr = stderr[-500:]
    stock = stock_state if stock_state is not None else stock_bwrap_profile_state()
    if exit_code == 0:
        cause = "ok"
    elif any(sig in stderr for sig in _STOCK_SIGNATURES) or stock.loaded is True:
        cause = "stock_profile_conflict"
    elif _NOT_LOADED_SIGNATURE in stderr:
        cause = "profile_not_loaded"
    else:
        cause = "failed"
    return AaExecProbe(
        ok=exit_code == 0, cause=cause, profile=profile, argv=tuple(argv),
        exit_code=exit_code, stderr=stderr, stock=stock,
    )


class AppArmorAttachUnavailable(SandboxUnavailable):
    """bwrap is usable but cannot attach the soffice AppArmor profile: refuse it."""

    def __init__(self, probe: AaExecProbe) -> None:
        super().__init__(probe.message())
        self.probe = probe
