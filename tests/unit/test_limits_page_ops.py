"""Tests for the size-aware page-op / shard memory budget (audit M4)."""
from __future__ import annotations

from clippyshot.limits import max_concurrent_page_ops
from clippyshot.rasterizer.base import _max_page_peak_mb


def test_oversized_page_collapses_concurrency_to_one():
    # A 14400pt (~5080mm) square page at 150 DPI is ~30000px → ~2.7-3.6 GB RGBA, far above the
    # 200 MB/page heuristic. On a 4 GB worker (2 GB usable) that must force shard/page-op
    # concurrency to 1 rather than fanning out N concurrent multi-GB renders.
    peak = _max_page_peak_mb([(5080.0, 5080.0)], dpi=150)
    assert peak > 2000.0, f"giant page peak underestimated: {peak} MB"
    assert max_concurrent_page_ops("4g", per_page_peak_mb=peak) == 1


def test_normal_pages_keep_default_concurrency():
    # A letter page (~216x279mm) at 150 DPI is ~8 MB — below the 200 MB default, so it must NOT
    # raise concurrency above the conservative baseline (per_page_peak_mb only ever lowers it).
    peak = _max_page_peak_mb([(215.9, 279.4)], dpi=150)
    assert peak < 200.0
    assert max_concurrent_page_ops("4g", per_page_peak_mb=peak) == max_concurrent_page_ops("4g")


def test_unknown_page_sizes_fall_back_to_default():
    assert _max_page_peak_mb(None, dpi=150) == 0.0
    assert _max_page_peak_mb([], dpi=150) == 0.0
    # 0.0 peak → default heuristic, unchanged.
    assert max_concurrent_page_ops("4g", per_page_peak_mb=0.0) == max_concurrent_page_ops("4g")


def test_largest_page_drives_the_budget():
    # A mixed doc (one giant sheet among normal pages) must be budgeted by the LARGEST page.
    peak = _max_page_peak_mb([(215.9, 279.4), (5080.0, 5080.0), (215.9, 279.4)], dpi=150)
    assert peak > 2000.0
    assert max_concurrent_page_ops("4g", per_page_peak_mb=peak) == 1
