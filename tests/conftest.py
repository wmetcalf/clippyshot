"""Shared pytest fixtures and configuration for ClippyShot."""
from __future__ import annotations

import shutil
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


def _sandbox_smoketest_passes(backend: str) -> bool:
    """Whether THIS host can run the sandbox we actually ship.

    Asks the backend itself rather than a hand-written approximation of it. The
    approximation drifted: the nsjail probe bind-mounted only `/usr`, so on any
    merged-usr distro -- Ubuntu 24.04, which is the fleet -- the loader at
    /lib64 was missing inside the jail and NOTHING dynamically linked could
    exec. That is `execve: No such file or directory`, which the probe reported
    as "cannot create user namespaces", telling operators to load an AppArmor
    profile that would not have helped. Four nsjail tests skipped on every such
    host, i.e. everywhere, while the real backend -- which recreates those
    symlinks inside the jail -- worked fine.
    """
    if not _have(backend):
        return False
    try:
        if backend == "bwrap":
            from clippyshot.sandbox.bwrap import BwrapSandbox as Sandbox
        else:
            from clippyshot.sandbox.nsjail import NsjailSandbox as Sandbox
        return Sandbox().smoketest().exit_code == 0
    except Exception:  # noqa: BLE001 - any failure means "cannot run here"
        return False


def _bwrap_can_create_userns() -> bool:
    return _sandbox_smoketest_passes("bwrap")


_BWRAP_USERNS_REASON = (
    "bwrap installed but cannot create user namespaces — "
    "load deploy/apparmor/clippyshot-bwrap (see deploy/apparmor/README.md)"
)

needs_bwrap_userns = pytest.mark.skipif(
    not _bwrap_can_create_userns(),
    reason=_BWRAP_USERNS_REASON,
)


def _nsjail_can_create_userns() -> bool:
    return _sandbox_smoketest_passes("nsjail")


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
