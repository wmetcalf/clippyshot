"""The soffice AppArmor profile is attached per-stage: ON for soffice, OFF for the
pdfium rasterizer (whose sys.prefix/venv the soffice profile can't describe)."""
import shutil

import pytest

from clippyshot.sandbox.base import SandboxRequest

_HAVE_AA_EXEC = shutil.which("aa-exec") is not None


def test_request_default_attaches_apparmor():
    assert SandboxRequest(argv=["x"]).attach_apparmor is True


def test_pdfium_opts_out_default_keeps_it():
    from clippyshot.rasterizer.base import ShardingRasterizer
    from clippyshot.rasterizer.pdfium import PdfiumRasterizer

    # base default = attach (covers pdftoppm); pdfium overrides to opt out
    assert ShardingRasterizer._attach_apparmor.__doc__ is not None
    pf = PdfiumRasterizer.__new__(PdfiumRasterizer)
    assert pf._attach_apparmor() is False


@pytest.mark.skipif(not _HAVE_AA_EXEC, reason="aa-exec not installed")
def test_bwrap_skips_aa_exec_when_opted_out():
    from clippyshot.sandbox.bwrap import BwrapSandbox

    sb = BwrapSandbox()
    on = sb._build_argv(SandboxRequest(argv=["/usr/bin/soffice"], attach_apparmor=True))
    off = sb._build_argv(SandboxRequest(argv=["/usr/bin/soffice"], attach_apparmor=False))
    assert "aa-exec" in " ".join(on)        # soffice stage: profile attached
    assert "aa-exec" not in " ".join(off)   # rasterizer stage: not attached


def test_nsjail_skips_proc_apparmor_when_opted_out():
    from clippyshot.sandbox.nsjail import NsjailSandbox

    sb = NsjailSandbox()
    if not sb._proc_apparmor_supported:
        pytest.skip("installed nsjail lacks --proc_apparmor")
    off = sb._build_argv(SandboxRequest(argv=["/usr/bin/soffice"], attach_apparmor=False))
    assert "--proc_apparmor" not in off
