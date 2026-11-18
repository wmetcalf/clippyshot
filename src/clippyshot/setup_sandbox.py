"""Detect — and optionally fix — the AppArmor user-namespace gate for bwrap/nsjail.

On Ubuntu 24.04+ `kernel.apparmor_restrict_unprivileged_userns=1` blocks the user
namespaces the `bwrap`/`nsjail` sandbox backends need; the scoped per-binary profiles in
`deploy/apparmor/` grant `userns` to *only* those binaries (the host-wide restriction stays
in force). This module **probes the actual capability** (runs the binary — no root needed to
detect) and either prints the exact `sudo` commands to load the profiles, or runs them with
``--apply``.

It NEVER sudos silently: the default is detect-and-print; ``--apply`` runs `sudo` (the
interactive password prompt is the operator's consent). Idempotent — a no-op once loaded.

It also detects Ubuntu's stock ``bwrap-userns-restrict`` profile (see
:mod:`clippyshot.sandbox.host_apparmor`), which stops ``aa-exec`` attaching
``clippyshot-soffice`` inside bwrap. Disabling a distro profile is a host-policy change, so
it takes its own explicit flag: without ``--disable-stock-bwrap-profile`` setup prints the
conflict and the exact commands and exits non-zero; with it, setup disables the profile
persistently (symlink into ``/etc/apparmor.d/disable/`` + ``apparmor_parser -R``), loads all
four ClippyShot profiles, and re-runs the attach probe to prove the fix.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from clippyshot.sandbox import host_apparmor

_RESTRICT_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
# (binary name, profile file basename) — the scoped userns-enablement profiles.
_BACKENDS = (("bwrap", "clippyshot-bwrap"), ("nsjail", "clippyshot-nsjail"))
# Every profile in deploy/apparmor/, in load order.
ALL_PROFILES = ("clippyshot-bwrap", "clippyshot-nsjail", "clippyshot-soffice", "clippyshot-runtime")
SOFFICE_PROFILE = "clippyshot-soffice"
_APPARMOR_D = Path("/etc/apparmor.d")


def _restrict_active() -> bool:
    try:
        return _RESTRICT_SYSCTL.read_text().strip() == "1"
    except OSError:
        return False  # sysctl absent ⇒ no restriction (older/other kernels)


def _userns_ok(binary: str, path: str) -> bool:
    """Probe whether the binary can actually create a user namespace (no root needed)."""
    try:
        if binary == "bwrap":
            r = subprocess.run(
                [path, "--unshare-user", "--uid", "0", "--ro-bind", "/", "/", "--", "/bin/true"],
                capture_output=True, timeout=15,
            )
            return r.returncode == 0
        if binary == "nsjail":
            from clippyshot.sandbox.nsjail import NsjailSandbox

            res = NsjailSandbox().smoketest()
            return res.exit_code == 0 and not res.killed
    except Exception:  # noqa: BLE001 — any probe failure ⇒ treat as not-working
        return False
    return False


@dataclass(frozen=True)
class ProfileAction:
    binary: str
    binary_path: str
    profile_name: str  # e.g. "clippyshot-bwrap"


@dataclass
class SetupReport:
    restrict_active: bool
    actions: list[ProfileAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # human lines for present/ok/absent
    # Ubuntu's stock bwrap-userns-restrict; None when bwrap is absent (nothing to conflict).
    stock: host_apparmor.StockBwrapProfileState | None = None
    aa_probe: host_apparmor.AaExecProbe | None = None

    @property
    def stock_conflict(self) -> bool:
        return self.stock is not None and self.stock.conflict


def diagnose() -> SetupReport:
    """Inspect the host and decide which scoped profiles (if any) need loading."""
    restrict = _restrict_active()
    rep = SetupReport(restrict_active=restrict)
    _diagnose_userns(rep, restrict)
    _diagnose_attach(rep)
    return rep


def _diagnose_attach(rep: SetupReport) -> None:
    """The stock bwrap profile, and whether aa-exec can attach clippyshot-soffice in bwrap."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return
    rep.stock = host_apparmor.stock_bwrap_profile_state()
    rep.notes.append(f"{host_apparmor.STOCK_BWRAP_PROFILE}: {rep.stock.describe()}"
                     + ("  <- CONFLICT" if rep.stock.conflict else ""))
    aa_exec = shutil.which("aa-exec")
    if aa_exec is None:
        rep.notes.append("aa-exec  absent (bwrap cannot attach clippyshot-soffice; "
                         "install apparmor-utils)")
        return
    probe = host_apparmor.probe_aa_exec(bwrap, aa_exec, SOFFICE_PROFILE, stock_state=rep.stock)
    rep.aa_probe = probe
    if probe.ok:
        rep.notes.append(f"aa-exec attach probe ({SOFFICE_PROFILE} inside bwrap): OK")
        return
    rep.notes.append(f"aa-exec attach probe ({SOFFICE_PROFILE} inside bwrap): FAILED "
                     f"[{probe.cause}] {probe.stderr}")
    if probe.cause == "profile_not_loaded" and not rep.stock_conflict:
        rep.actions.append(ProfileAction("aa-exec", aa_exec, SOFFICE_PROFILE))
    elif not rep.stock_conflict:
        # The probe stops at the first failure: on a fresh host bwrap's own userns fails
        # before aa-exec runs, so a missing clippyshot-soffice would only show on a second
        # pass. Schedule it now when the kernel's profile list says it is not loaded.
        names = host_apparmor.loaded_profile_names()
        if names is not None and SOFFICE_PROFILE not in names:
            rep.actions.append(ProfileAction("aa-exec", aa_exec, SOFFICE_PROFILE))


def _diagnose_userns(rep: SetupReport, restrict: bool) -> None:
    for binary, profile in _BACKENDS:
        path = shutil.which(binary)
        if path is None:
            rep.notes.append(f"{binary:<7} absent (backend unavailable)")
            continue
        if _userns_ok(binary, path):
            rep.notes.append(f"{binary:<7} {path}  userns: OK")
        elif restrict:
            rep.actions.append(ProfileAction(binary, path, profile))
            rep.notes.append(f"{binary:<7} {path}  userns: BLOCKED -> load {profile}")
        else:
            rep.notes.append(f"{binary:<7} {path}  userns: failing (not the AppArmor restriction)")


def default_profile_dir() -> Path:
    """Where the clippyshot-{bwrap,nsjail} profiles live.

    Prefers the repo layout (`deploy/apparmor/`, the common bare-metal/dev case where
    setup-sandbox is most useful); falls back to a packaged install location. Pass
    `--profile-dir` to override either."""
    candidates = (
        Path(__file__).resolve().parents[2] / "deploy" / "apparmor",  # repo / editable install
        Path("/etc/clippyshot/apparmor"),                              # packaged install
        Path("/usr/share/clippyshot/apparmor"),
    )
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]  # repo path — the report surfaces "not found" + suggests --profile-dir


def commands_for(actions: list[ProfileAction], profile_dir: Path) -> list[list[str]]:
    """The exact `sudo` argv list that loads the needed scoped profiles."""
    cmds: list[list[str]] = []
    for a in actions:
        src = str(profile_dir / a.profile_name)
        cmds.append(["sudo", "cp", src, "/etc/apparmor.d/"])
        cmds.append(["sudo", "apparmor_parser", "-r", "-W", f"/etc/apparmor.d/{a.profile_name}"])
    return cmds


def apply(actions: list[ProfileAction], profile_dir: Path) -> int:
    """Run the load commands via sudo (interactive). Returns 0 on success."""
    for cmd in commands_for(actions, profile_dir):
        print("+ " + shlex.join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            return rc
    return 0


# --- the stock bwrap-userns-restrict profile ----------------------------------------------


class StockProfileUnmanaged(RuntimeError):
    """The stock profile is loaded but there is no file in /etc/apparmor.d to unload it by."""


@dataclass(frozen=True)
class HostCommand:
    argv: list[str]
    # Only for `apparmor_parser -R` when the load state could not be read: unloading a
    # profile that turns out not to be loaded is not a failure of the fix.
    allow_failure: bool = False


def stock_fix_plan(
    stock: host_apparmor.StockBwrapProfileState,
    profile_dir: Path,
    *,
    apparmor_d: Path = _APPARMOR_D,
) -> list[HostCommand]:
    """Commands that disable bwrap-userns-restrict persistently, then load all four
    ClippyShot profiles. Each step is skipped when already done, so a re-run is a no-op
    up to the (idempotent) profile reload."""
    name = host_apparmor.STOCK_BWRAP_PROFILE
    src = str(apparmor_d / name)
    if stock.loaded is True and not stock.installed:
        loaded = ", ".join(sorted(host_apparmor.STOCK_BWRAP_PROFILE_NAMES))
        raise StockProfileUnmanaged(
            f"profiles {loaded} are loaded but {src} does not exist, so there is no file to "
            "disable or unload them by. Find where they were loaded from (e.g. a package "
            "under /usr/share/apparmor or a custom policy dir), remove them with "
            "`sudo apparmor_parser -R <that file>`, then re-run setup-sandbox."
        )
    cmds: list[HostCommand] = []
    if stock.installed and not stock.disabled:
        cmds.append(HostCommand(["sudo", "ln", "-sf", src, str(apparmor_d / "disable" / name)]))
    if stock.installed and stock.loaded is not False:
        cmds.append(HostCommand(["sudo", "apparmor_parser", "-R", src],
                                allow_failure=stock.loaded is None))
    for p in ALL_PROFILES:
        cmds.append(HostCommand(["sudo", "cp", str(profile_dir / p), f"{apparmor_d}/"]))
        cmds.append(HostCommand(["sudo", "apparmor_parser", "-r", "-W", f"{apparmor_d}/{p}"]))
    return cmds


def _run_host_command(argv: list[str]) -> int:
    """The one place setup runs a host command (the tests' seam)."""
    return subprocess.run(argv).returncode


def apply_plan(plan: list[HostCommand]) -> int:
    for c in plan:
        print("+ " + shlex.join(c.argv))
        rc = _run_host_command(c.argv)
        if rc != 0:
            if c.allow_failure:
                print(f"  (exit {rc}; tolerated: the profile was probably not loaded)")
                continue
            return rc
    return 0


def verify_attach() -> tuple[bool, list[str]]:
    """After a fix: is the stock profile out of the way, and does the attach probe pass?"""
    stock = host_apparmor.stock_bwrap_profile_state()
    ok = not stock.conflict
    lines = [f"{host_apparmor.STOCK_BWRAP_PROFILE}: {stock.describe()}"]
    bwrap, aa_exec = shutil.which("bwrap"), shutil.which("aa-exec")
    if bwrap and aa_exec:
        probe = host_apparmor.probe_aa_exec(bwrap, aa_exec, SOFFICE_PROFILE, stock_state=stock)
        lines.append("aa-exec attach probe: OK" if probe.ok else probe.message())
        ok = ok and probe.ok
    else:
        missing = " and ".join(n for n, p in (("bwrap", bwrap), ("aa-exec", aa_exec)) if not p)
        lines.append(f"aa-exec attach probe: not run ({missing} not installed; install "
                     "bubblewrap/apparmor)")
        ok = False
    return ok, lines
