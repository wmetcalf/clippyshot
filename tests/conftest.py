"""Shared pytest fixtures and configuration for ClippyShot."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture
def tmp_outdir(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


needs_bwrap = pytest.mark.skipif(not _have("bwrap"), reason="bubblewrap not installed")
needs_nsjail = pytest.mark.skipif(not _have("nsjail"), reason="nsjail not installed")
needs_soffice = pytest.mark.skipif(not _have("soffice"), reason="LibreOffice not installed")
needs_pdftoppm = pytest.mark.skipif(not _have("pdftoppm"), reason="poppler-utils not installed")


def _bwrap_can_create_userns() -> bool:
    """Probe whether bwrap can actually create a user namespace on this host.

    On Ubuntu 24.04+ with kernel.apparmor_restrict_unprivileged_userns=1 and
    no clippyshot-bwrap AppArmor profile loaded, this returns False.
    """
    if not _have("bwrap"):
        return False
    try:
        proc = subprocess.run(
            ["bwrap", "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


_BWRAP_USERNS_REASON = (
    "bwrap installed but cannot create user namespaces — "
    "load deploy/apparmor/clippyshot-bwrap (see deploy/apparmor/README.md)"
)

needs_bwrap_userns = pytest.mark.skipif(
    not _bwrap_can_create_userns(),
    reason=_BWRAP_USERNS_REASON,
)


def _nsjail_can_create_userns() -> bool:
    """Probe whether nsjail can actually create a user namespace on this host.

    Mirror of _bwrap_can_create_userns for the nsjail backend. On Ubuntu
    24.04+ with kernel.apparmor_restrict_unprivileged_userns=1 and no
    clippyshot-nsjail AppArmor profile loaded, this returns False.
    """
    if not _have("nsjail"):
        return False
    try:
        proc = subprocess.run(
            ["nsjail", "--mode", "o", "--quiet", "--really_quiet",
             "--disable_proc", "--iface_no_lo", "--user", "65534", "--group", "65534",
             "--bindmount_ro", "/usr:/usr",
             "--", "/bin/true"],
            capture_output=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


_NSJAIL_USERNS_REASON = (
    "nsjail installed but cannot create user namespaces — "
    "load deploy/apparmor/clippyshot-nsjail (see deploy/apparmor/README.md)"
)

needs_nsjail_userns = pytest.mark.skipif(
    not _nsjail_can_create_userns(),
    reason=_NSJAIL_USERNS_REASON,
)


def _any_sandbox_available() -> bool:
    """Return True if at least one sandbox backend passes its smoketest."""
    from pathlib import Path as _Path
    # Container check: inside a container we're a valid sandbox.
    if _Path("/.dockerenv").exists() or _Path("/run/.containerenv").exists():
        import os
        if os.geteuid() != 0:
            return True
    return _bwrap_can_create_userns() or _nsjail_can_create_userns()


needs_any_sandbox = pytest.mark.skipif(
    not _any_sandbox_available(),
    reason="no sandbox backend available on this host (nsjail/bwrap need AppArmor userns profiles; not inside a container)",
)
