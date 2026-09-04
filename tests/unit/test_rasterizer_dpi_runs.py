"""An oversized page must not drag the pages beside it down.

The pixel budget is a per-PAGE constraint, but the dpi decision is made per
render INVOCATION. Before pages were grouped by their clamped dpi, one 14400 pt
sheet took every page sharing its invocation down to ~50 dpi: an entire
document below the sharding threshold, and whichever ordinary pages the even
split happened to put in the giant's shard.
"""
from __future__ import annotations


import pytest

from clippyshot.limits import max_page_px
from clippyshot.rasterizer import base as B

A4 = (210.0, 297.0)
GIANT = (5080.0, 5080.0)  # a 14400 pt square sheet, pdfium's MediaBox cap
FULL_DPI = 150


class _Probe(B.ShardingRasterizer):
    name = "probe"

    def _build_argv(self, *a, **k):
        return []


def _invocations(monkeypatch, tmp_path, pages, dpi=FULL_DPI):
    """What each render invocation is asked to do, driving the REAL entry point.

    Goes through `rasterize()` rather than reproducing its wiring: a helper that
    calls `_dpi_runs` itself would still pass if `rasterize` stopped calling it,
    which is exactly the regression this guards.
    """
    calls: list[tuple[int, int, float]] = []

    def spy(self, *, sandbox_pdf, out_dir, dpi, first, last, pdf_parent,
            page_sizes_mm=None, budget_px=0):
        sizes = page_sizes_mm[first - 1:last] if page_sizes_mm else None
        calls.append((first, last, B._effective_dpi(dpi, sizes, budget_px)))
        for page in range(first, last + 1):
            (out_dir / f"page-{page:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(B.ShardingRasterizer, "_run_range", spy)
    # The metadata pass reads the PNGs it was handed; stub it out, this test is
    # about which ranges get rendered at which dpi.
    monkeypatch.setattr(
        B, "_page_meta_from_png", lambda *a, **k: (0, 0), raising=False
    )
    pdf = tmp_path / "in" / "x.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    inst = _Probe(sandbox=None)
    try:
        inst.rasterize(pdf, out, dpi=dpi, max_pages=len(pages), page_sizes_mm=pages)
    except Exception as exc:  # noqa: BLE001 - post-render metadata is not under test
        if not calls:
            raise AssertionError(f"rasterize never rendered anything: {exc}") from exc
    return sorted(calls)


@pytest.mark.parametrize(
    ("label", "pages"),
    [
        ("below the sharding threshold", [A4] * 2 + [GIANT]),
        ("giant last, absorbed by the remainder shard", [A4] * 11 + [GIANT]),
        ("giant first", [GIANT] + [A4] * 11),
    ],
)
def test_an_oversized_page_does_not_reduce_the_pages_beside_it(
    monkeypatch, tmp_path, label, pages
):
    calls = _invocations(monkeypatch, tmp_path, pages)
    giant_pages = {i for i, size in enumerate(pages, 1) if size == GIANT}
    reduced = {
        page
        for first, last, used in calls
        if used < FULL_DPI
        for page in range(first, last + 1)
    }

    assert reduced == giant_pages, (
        f"{label}: only the oversized page may render below {FULL_DPI} dpi; "
        f"reduced={sorted(reduced)} oversized={sorted(giant_pages)}"
    )
    # ...and it still IS reduced -- the clamp is what stops it OOM'ing the guest.
    assert reduced, "the oversized page must still be clamped"
    assert all(first <= last for first, last, _ in calls), "ranges must be well-formed"


def test_every_page_is_covered_exactly_once(monkeypatch, tmp_path):
    """Splitting by dpi must not drop or duplicate a page."""
    pages = [A4] * 5 + [GIANT] + [A4] * 6
    covered: list[int] = []
    for first, last, _ in _invocations(monkeypatch, tmp_path, pages):
        covered.extend(range(first, last + 1))
    assert sorted(covered) == list(range(1, len(pages) + 1)), covered


def test_a_uniform_document_is_unchanged(monkeypatch, tmp_path):
    """The common case must take exactly the path it took before.

    `_dpi_runs` yields one run when every page clamps alike, so the sharding
    below it is the same work, invocation for invocation.
    """
    pages = [A4] * 12
    assert B._dpi_runs(pages, FULL_DPI, max_page_px()) == [(1, 12)]
    calls = _invocations(monkeypatch, tmp_path, pages)
    assert all(d == FULL_DPI for _, _, d in calls)
    covered: list[int] = []
    for first, last, _ in calls:
        covered.extend(range(first, last + 1))
    assert sorted(covered) == list(range(1, 13))


def test_alternating_sizes_still_render_concurrently(monkeypatch, tmp_path):
    """Grouping by dpi must not serialise the document.

    A workbook alternating normal and giant sheets becomes many single-page
    runs. Running each run to completion before starting the next would launch
    them one after another -- fast enough to miss a worker deadline the old
    sharded path met -- so every range goes through ONE bounded pool.
    """
    import threading
    import time

    pages = [A4 if i % 2 == 0 else GIANT for i in range(12)]
    threads: set[int] = set()
    overlap = threading.Barrier(2, timeout=5)

    def spy(self, *, sandbox_pdf, out_dir, dpi, first, last, pdf_parent,
            page_sizes_mm=None, budget_px=0):
        threads.add(threading.get_ident())
        try:  # two ranges must be in flight at once for this to pass
            overlap.wait()
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.01)
        for page in range(first, last + 1):
            (out_dir / f"page-{page:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(B.ShardingRasterizer, "_run_range", spy)
    pdf = tmp_path / "in" / "x.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    inst = _Probe(sandbox=None)
    try:
        inst.rasterize(pdf, tmp_path / "out", dpi=FULL_DPI, max_pages=len(pages),
                       page_sizes_mm=pages)
    except Exception:  # noqa: BLE001 - post-render metadata is not under test
        pass
    assert len(threads) > 1, (
        "alternating sizes must still render in parallel, not one run at a time"
    )


def test_concurrency_never_exceeds_the_document_memory_budget(monkeypatch, tmp_path):
    """The ceiling that keeps N concurrent renders from OOM'ing the guest.

    Ranges from different dpi-runs share one pool, so the bound has to come
    from the whole document's clamped peak -- a per-run figure would let a run
    of A4s widen the pool while a giant's range is still in flight.
    """
    import os as _os
    import threading
    import time

    from clippyshot.limits import max_concurrent_page_ops

    pages = [GIANT if i % 3 == 0 else A4 for i in range(24)]
    budget = max_page_px()
    expected = min(
        max(1, (_os.cpu_count() or 2) // 2),
        max_concurrent_page_ops(
            per_page_peak_mb=B._max_page_peak_mb(pages, FULL_DPI, cap_px=budget)
        ),
    )

    lock = threading.Lock()
    live = 0
    peak = 0

    def spy(self, *, sandbox_pdf, out_dir, dpi, first, last, pdf_parent,
            page_sizes_mm=None, budget_px=0):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        for page in range(first, last + 1):
            (out_dir / f"page-{page:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(B.ShardingRasterizer, "_run_range", spy)
    pdf = tmp_path / "in" / "x.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    try:
        _Probe(sandbox=None).rasterize(
            pdf, tmp_path / "out", dpi=FULL_DPI, max_pages=len(pages), page_sizes_mm=pages
        )
    except Exception:  # noqa: BLE001 - post-render metadata is not under test
        pass
    assert peak <= expected, (
        f"{peak} concurrent renders exceeded the document budget of {expected}"
    )
