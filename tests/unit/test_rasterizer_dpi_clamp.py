"""Per-page pixel-budget DPI clamp.

Sharding bounds how many pages render CONCURRENTLY, but nothing bounded the size
of a SINGLE page: a 14400pt SinglePageSheets sheet at 150 DPI is ~30000px/side →
billions of pixels → one multi-GB RGBA bitmap that OOM-wedges a memory-capped warm
guest. A wedged guest makes zero progress and burns the whole worker timeout
(observed as `warm worker timed out after 300s`). The rasterizer now clamps render
DPI so the largest page in each shard fits a memory-derived pixel budget — a valid
downscaled image instead of an OOM.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clippyshot.limits import MAX_POSTPROCESS_PX, max_page_px
from clippyshot.rasterizer.base import _effective_dpi
from clippyshot.trimmer import _MAX_VECTOR_PIXELS
from clippyshot.sandbox.base import SandboxRequest
from clippyshot.types import SandboxResult

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "safe" / "two_page.pdf"

_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


# --- max_page_px (memory-derived budget) ----------------------------------

def test_max_page_px_derived_from_worker_memory(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_MAX_PAGE_PX", raising=False)
    # Below the post-process cap the budget scales with RAM (monotonic): a 2 GB
    # worker (~1/8 = 64 MP) budgets more than a 1 GB one (~32 MP).
    assert max_page_px("2g") > max_page_px("1g")
    # At/above 4 GB the ~1/8-of-RAM figure exceeds the post-process cap, so it
    # pins there — a rendered page must stay trim/focus/scan-eligible.
    assert max_page_px("4g") == MAX_POSTPROCESS_PX
    assert max_page_px("8g") == MAX_POSTPROCESS_PX


def test_render_budget_never_exceeds_postprocess_budget(monkeypatch):
    """A page we RENDER must always be one the derivative pipeline can consume —
    otherwise giant SinglePageSheets spreadsheets (the reason trim/focus exists)
    render but silently get no focused crop. The derived budget must never exceed
    the trimmer's own materialization guard."""
    monkeypatch.delenv("CLIPPYSHOT_MAX_PAGE_PX", raising=False)
    assert _MAX_VECTOR_PIXELS == MAX_POSTPROCESS_PX
    for mem in ("2g", "4g", "8g", "16g"):
        assert max_page_px(mem) <= _MAX_VECTOR_PIXELS


def test_max_page_px_env_override(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGE_PX", "5000000")
    assert max_page_px("4g") == 5_000_000


def test_max_page_px_has_floor_and_ceiling(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_MAX_PAGE_PX", raising=False)
    # Absurdly tiny worker still yields a usable floor (never 0 / negative).
    assert max_page_px("16m") >= 1_000_000
    # Absurd env can't disable the guard or wrap to nonsense.
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGE_PX", "0")
    assert max_page_px("4g") >= 1_000_000
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGE_PX", "999999999999")
    assert max_page_px("4g") <= 200_000_000


# --- _effective_dpi (the clamp) -------------------------------------------

def test_effective_dpi_unchanged_for_normal_page():
    # Letter (216x279mm) at 150 DPI is ~2 MP — far under any budget.
    assert _effective_dpi(150, [(215.9, 279.4)], max_page_px=128_000_000) == 150


def test_effective_dpi_clamped_for_giant_page():
    # 14400pt (~5080mm) square at 150 DPI is ~900 MP >> 128 MP budget → DPI drops.
    eff = _effective_dpi(150, [(5080.0, 5080.0)], max_page_px=128_000_000)
    assert eff < 150
    # And the clamped DPI must actually bring the page under budget.
    w = h = (5080.0 / 25.4) * eff
    assert w * h <= 128_000_000 * 1.01
    # Never below the legibility/Limits floor of 36.
    assert eff >= 36


def test_effective_dpi_largest_page_in_range_drives_it():
    sizes = [(215.9, 279.4), (5080.0, 5080.0), (215.9, 279.4)]
    assert _effective_dpi(150, sizes, max_page_px=128_000_000) < 150


def test_effective_dpi_noop_when_sizes_unknown_or_budget_disabled():
    assert _effective_dpi(150, None, max_page_px=128_000_000) == 150
    assert _effective_dpi(150, [], max_page_px=128_000_000) == 150
    assert _effective_dpi(150, [(5080.0, 5080.0)], max_page_px=0) == 150


# --- integration: the clamp reaches the render argv -----------------------

class _CapturingSandbox:
    name = "capture"

    def __init__(self) -> None:
        self.last_request: SandboxRequest | None = None

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.last_request = request
        out_host = next(
            m.host_path for m in request.rw_mounts if m.sandbox_path == Path("/sandbox/out")
        )
        (out_host / "page-1.png").write_bytes(_TINY_PNG)
        (out_host / "page-2.png").write_bytes(_TINY_PNG)
        return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1, killed=False)

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


def _scale(argv: list[str]) -> float:
    return float(argv[argv.index("--scale") + 1])


def test_pdfium_render_scale_clamped_for_giant_page(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGE_PX", "128000000")
    from clippyshot.rasterizer.pdfium import PdfiumRasterizer

    sb = _CapturingSandbox()
    PdfiumRasterizer(sandbox=sb).rasterize(
        FIXTURE, tmp_path / "out", dpi=150, max_pages=2,
        page_sizes_mm=[(5080.0, 5080.0), (5080.0, 5080.0)],
    )
    # 150 DPI → scale 2.083; clamped for the giant page → strictly smaller.
    assert _scale(sb.last_request.argv) < 2.083


def test_pdfium_render_scale_untouched_for_normal_pages(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGE_PX", "128000000")
    from clippyshot.rasterizer.pdfium import PdfiumRasterizer

    sb = _CapturingSandbox()
    PdfiumRasterizer(sandbox=sb).rasterize(
        FIXTURE, tmp_path / "out", dpi=150, max_pages=2,
        page_sizes_mm=[(215.9, 279.4), (215.9, 279.4)],
    )
    assert _scale(sb.last_request.argv) == pytest.approx(150 / 72.0)
