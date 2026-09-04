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


def _host_allows_userns(argv: list[str]) -> bool:
    """Whether THIS HOST permits the unprivileged user namespace these need.

    Deliberately NOT our backend's own smoketest: gating a backend's tests on
    that backend working makes a regression skip the very tests meant to catch
    it, so a broken mount or seccomp argument would read as "unsupported host".
    This asks the external tool to do the one thing the host can forbid --
    create a userns and exec -- with the whole root bound read-only.

    The root bind matters: the previous probe mounted only `/usr`, so on a
    merged-usr distro the loader at /lib64 was missing inside the jail and
    NOTHING could exec. That is a probe bug, and it read as a host limitation.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=10)
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _bwrap_can_create_userns() -> bool:
    if not _have("bwrap"):
        return False
    return _host_allows_userns(
        ["bwrap", "--unshare-user", "--die-with-parent", "--ro-bind", "/", "/",
         "--", "/bin/true"]
    )


_BWRAP_USERNS_REASON = (
    "bwrap installed but cannot create user namespaces — "
    "load deploy/apparmor/clippyshot-bwrap (see deploy/apparmor/README.md)"
)

needs_bwrap_userns = pytest.mark.skipif(
    not _bwrap_can_create_userns(),
    reason=_BWRAP_USERNS_REASON,
)


def _nsjail_can_create_userns() -> bool:
    if not _have("nsjail"):
        return False
    return _host_allows_userns(
        ["nsjail", "--mode", "o", "--quiet", "--really_quiet", "--disable_proc",
         "--iface_no_lo", "--user", "65534", "--group", "65534",
         "--bindmount_ro", "/:/", "--", "/bin/true"]
    )


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
