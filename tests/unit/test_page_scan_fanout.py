"""The per-page scan fan-out is bounded by memory, not just CPU.

`convert()` loads a full-page RGB buffer per worker for hash/trim/focus, so the
fan-out costs ~N x page_size RAM. A giant page (~30000px, ~2 GB RGBA) has to
collapse it to one worker rather than run N concurrent multi-GB decodes -- the
gVisor warm tier has no per-worker memory cgroup, so an unbounded fan-out is a
host-memory DoS there.

Nothing tested it, because nothing could: the computation was inline in
convert(), reachable only through a full conversion. Dropping
`max_concurrent_page_ops` from the min() left the entire suite green, and so did
taking the SMALLEST page instead of the largest.
"""
import pytest

from clippyshot.converter import _page_scan_workers


class _Page:
    def __init__(self, w: int, h: int) -> None:
        self.width_px, self.height_px = w, h


A4 = _Page(1275, 1650)          # ~2 MP, ~8 MB RGBA
GIANT = _Page(30000, 30000)     # ~900 MP, ~3.4 GB RGBA


@pytest.fixture(autouse=True)
def _pinned_worker_memory(monkeypatch):
    """The budget reads CLIPPYSHOT_WORKER_MEMORY (then the cgroup limit, then
    MemTotal). Unpinned, a large host raises the ceiling until the memory bound
    stops binding and these assertions measure nothing."""
    monkeypatch.setenv("CLIPPYSHOT_WORKER_MEMORY", "4g")


def test_a_giant_page_collapses_the_fanout_to_one():
    assert _page_scan_workers([GIANT] * 16, cpu_count=8) == 1


def test_ordinary_pages_still_use_the_cpu_budget():
    # The positive control: without it, "always 1" would pass the test above.
    assert _page_scan_workers([A4] * 16, cpu_count=8) == 8


def test_the_largest_page_in_the_run_decides():
    # One giant page among small ones is what costs the memory, so the run is
    # budgeted for it -- taking the smallest instead was a surviving mutant.
    mixed = [A4] * 15 + [GIANT]
    assert _page_scan_workers(mixed, cpu_count=8) == 1


def test_the_page_count_and_cpu_count_still_bound_it():
    assert _page_scan_workers([A4] * 2, cpu_count=8) == 2   # fewer pages than CPUs
    assert _page_scan_workers([A4] * 16, cpu_count=1) == 1  # fewer CPUs than pages


def test_no_pages_is_one_worker_not_zero():
    """A zero here would mean ThreadPoolExecutor(max_workers=0), which raises."""
    assert _page_scan_workers([]) == 1
