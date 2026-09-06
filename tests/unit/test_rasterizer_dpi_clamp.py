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

from clippyshot.limits import MAX_POSTPROCESS_PX, max_concurrent_page_ops, max_page_px
from clippyshot.rasterizer.base import _effective_dpi, _max_page_peak_mb
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


def test_effective_dpi_never_falls_below_the_floor():
    """The floor has to be REACHED to be tested.

    test_effective_dpi_clamped_for_giant_page asserts `eff >= 36`, but a 5080mm
    page against the default 128 MP budget scales to 56 DPI on its own -- the
    floor never engages, so that assertion holds with the floor deleted. Measured:
    removing `max(_MIN_DPI, ...)` left the entire suite green.

    A smaller budget is the documented regime where the floor does the work (the
    function's own docstring: "a CLIPPYSHOT_MAX_PAGE_PX override below ~52 MP ...
    in that regime 36 DPI is the best we can do"). Unfloored, these would be:

        max_page_px= 51,000,000  ->  35
        max_page_px= 20,000,000  ->  22
        max_page_px=  1,000,000  ->   5
        max_page_px=          1  ->   0     <- pdftoppm -r 0
    """
    giant = [(5080.0, 5080.0)]
    for budget in (51_000_000, 20_000_000, 1_000_000, 1):
        assert _effective_dpi(150, giant, max_page_px=budget) == 36, budget
    # The floor is a floor, not a constant: a budget that permits more still gets more.
    assert _effective_dpi(150, giant, max_page_px=128_000_000) > 36


def test_effective_dpi_largest_page_in_range_drives_it():
    sizes = [(215.9, 279.4), (5080.0, 5080.0), (215.9, 279.4)]
    assert _effective_dpi(150, sizes, max_page_px=128_000_000) < 150


def test_effective_dpi_noop_when_sizes_unknown_or_budget_disabled():
    assert _effective_dpi(150, None, max_page_px=128_000_000) == 150
    assert _effective_dpi(150, [], max_page_px=128_000_000) == 150
    assert _effective_dpi(150, [(5080.0, 5080.0)], max_page_px=0) == 150


# --- the shard planner itself (nothing referenced _shard_run before) ----------

def test_shard_count_is_bounded_by_the_memory_budget_not_just_cpu(monkeypatch):
    """Sharding must respect the memory budget the run's own pages imply.

    `_shard_run` had no test at all: dropping `mem_budget` from
    `min(cpu_budget, mem_budget, run_pages)` left the entire suite green, and the
    result is more concurrent renders than the worker's memory can hold -- which
    is the OOM this budget exists to prevent, on the gVisor warm tier that has no
    per-worker cgroup to catch it.

    Both bounds are asserted, so this cannot pass by always returning one shard.

    The worker memory is PINNED rather than inherited: max_concurrent_page_ops
    reads CLIPPYSHOT_WORKER_MEMORY (falling back to the cgroup limit / MemTotal),
    and at 8g or more it returns its ceiling of 8 for this page's peak -- the
    memory bound stops binding and the test measures nothing on a large host
    (codex). At 4g it binds at 4, which is the case this test is about.
    """
    from clippyshot.limits import max_concurrent_page_ops
    from clippyshot.rasterizer.base import ShardingRasterizer, _max_page_peak_mb

    monkeypatch.setenv("CLIPPYSHOT_WORKER_MEMORY", "4g")

    class _Planner(ShardingRasterizer):
        def _build_argv(self, *a, **kw):  # pragma: no cover - never invoked
            raise AssertionError("planning must not build a command line")

    planner = _Planner.__new__(_Planner)  # planning is pure; no sandbox needed
    giant = [(5080.0, 5080.0)] * 16
    budget_px = 128_000_000
    mem_budget = max_concurrent_page_ops(
        per_page_peak_mb=_max_page_peak_mb(giant, 150, cap_px=budget_px)
    )
    assert mem_budget < 8, "pick pages whose memory budget actually binds"

    ranges = planner._shard_run(
        first=1, last=16, dpi=150, page_sizes_mm=giant,
        budget_px=budget_px, cpu_budget=8,
    )
    assert len(ranges) <= mem_budget, (
        f"{len(ranges)} shards for pages whose memory budget allows {mem_budget}"
    )
    assert ranges[0][0] == 1 and ranges[-1][1] == 16, ranges

    # Small pages: the memory budget stops binding and the CPU budget takes over,
    # so this is not passing merely by refusing to shard.
    small = [(215.9, 279.4)] * 16
    small_ranges = planner._shard_run(
        first=1, last=16, dpi=150, page_sizes_mm=small,
        budget_px=budget_px, cpu_budget=8,
    )
    assert len(small_ranges) > len(ranges), (small_ranges, ranges)


# --- F4: a clamped giant page must not collapse the shard-concurrency budget ---

def test_max_page_peak_mb_cap_bounds_the_giant_page():
    """Without a cap, a 14400pt page peaks at multiple GB; with cap_px it reflects
    the CLAMPED render size (<= budget), which is what actually gets allocated."""
    uncapped = _max_page_peak_mb([(5080.0, 5080.0)], dpi=150)
    capped = _max_page_peak_mb([(5080.0, 5080.0)], dpi=150, cap_px=100_000_000)
    assert uncapped > 2000.0                      # ~3.4 GB unclamped
    assert capped < uncapped
    assert capped <= 100_000_000 * 4 / (1024 * 1024) + 1  # ~381 MB (100 MP RGBA)


def test_clamped_giant_page_does_not_collapse_shard_budget():
    """The bug: sizing shard_count on the UNCLAMPED giant peak collapses it to 1,
    forcing the single-shot path to clamp the WHOLE doc. Budgeting on the clamped
    peak keeps concurrency > 1 so normal-page shards render at full resolution."""
    sizes = [(215.9, 279.4)] * 4 + [(5080.0, 5080.0)]  # 4 normal + 1 giant
    uncapped_peak = _max_page_peak_mb(sizes, dpi=150)
    capped_peak = _max_page_peak_mb(sizes, dpi=150, cap_px=100_000_000)
    assert max_concurrent_page_ops("4g", per_page_peak_mb=uncapped_peak) == 1   # old: collapse
    assert max_concurrent_page_ops("4g", per_page_peak_mb=capped_peak) > 1       # fixed: shards survive


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
