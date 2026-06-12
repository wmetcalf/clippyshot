"""Detect — and optionally fix — the AppArmor user-namespace gate for bwrap/nsjail.

On Ubuntu 24.04+ `kernel.apparmor_restrict_unprivileged_userns=1` blocks the user
namespaces the `bwrap`/`nsjail` sandbox backends need; the scoped per-binary profiles in
`deploy/apparmor/` grant `userns` to *only* those binaries (the host-wide restriction stays
in force). This module **probes the actual capability** (runs the binary — no root needed to
detect) and either prints the exact `sudo` commands to load the profiles, or runs them with
``--apply``.

It NEVER sudos silently: the default is detect-and-print; ``--apply`` runs `sudo` (the
interactive password prompt is the operator's consent). Idempotent — a no-op once loaded.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_RESTRICT_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
# (binary name, profile file basename) — the scoped userns-enablement profiles.
_BACKENDS = (("bwrap", "clippyshot-bwrap"), ("nsjail", "clippyshot-nsjail"))


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


def diagnose() -> SetupReport:
    """Inspect the host and decide which scoped profiles (if any) need loading."""
    restrict = _restrict_active()
    rep = SetupReport(restrict_active=restrict)
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
    return rep


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
        print("+ " + " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            return rc
    return 0
