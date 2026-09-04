"""Rasterizer protocol + shared sharding base for CLI-backed rasterizers."""
from __future__ import annotations

import logging
import math
import functools
import os
import re
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image
from pypdf import PdfReader

from clippyshot._argv import assert_positional as _assert_positional
from clippyshot.errors import RasterizeError
from clippyshot.limits import Limits, max_concurrent_page_ops, max_page_px
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.types import RasterizedPage

_log = logging.getLogger(__name__)

_PT_PER_INCH = 72.0
_MM_PER_INCH = 25.4
# Page-count threshold below which sharding adds more subprocess
# overhead than it saves.
_MIN_PAGES_FOR_SHARDING = 4

_SANDBOX_IN = Path("/sandbox/in")
_SANDBOX_OUT = Path("/sandbox/out")


def _dpi_runs(
    page_sizes_mm: list[tuple[float, float]] | None, dpi: int, budget_px: int
) -> list[tuple[int, int]]:
    """Maximal runs of consecutive pages that clamp to the SAME dpi.

    The pixel budget is a per-PAGE constraint, but the dpi decision is made per
    render INVOCATION, so one oversized page dragged every page it shared an
    invocation with down to its own reduced dpi. Measured on a 14400 pt sheet
    among A4s: a document below the sharding threshold rendered ENTIRELY at
    50 dpi instead of 150, and above it the shard that absorbed the remainder
    took three ordinary pages down with the giant.

    Splitting on the clamped dpi keeps that reduction where it belongs. Runs of
    equal dpi are contiguous page ranges because the engines render ranges, not
    page lists; a document whose pages all clamp alike -- which is nearly all of
    them -- yields exactly one run and the previous behaviour, invocation for
    invocation.
    """
    if not page_sizes_mm:
        return []
    per_page = [_effective_dpi(dpi, [size], budget_px) for size in page_sizes_mm]
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(per_page) + 1):
        if index == len(per_page) or per_page[index] != per_page[start]:
            runs.append((start + 1, index))
            start = index
    return runs


def _max_page_peak_mb(
    page_sizes_mm: list[tuple[float, float]] | None, dpi: int, cap_px: int = 0
) -> float:
    """Worst-case in-RAM RGBA buffer (MB) of the LARGEST page at ``dpi``.

    Returns 0.0 when page sizes are unknown (callers then fall back to the
    default per-page heuristic). 4 bytes/px (RGBA) is the conservative peak; the
    PNG encoder also holds the decoded buffer during the giant-page render.

    ``cap_px`` (>0) caps each page's pixel count at the value the rasterizer will
    actually RENDER it at (the per-page DPI clamp downscales oversized pages to
    ``cap_px``). Sizing the shard-concurrency budget on the CLAMPED peak — not the
    unclamped mediabox — stops a single oversized page from collapsing
    ``shard_count`` to 1 (which would route the whole doc through the single-shot
    path and clamp EVERY page, not just the oversized one). Safe because the
    clamp bounds the actual render, so N concurrent clamped renders stay bounded.
    """
    if not page_sizes_mm:
        return 0.0
    peak = 0.0
    for w_mm, h_mm in page_sizes_mm:
        w_px = (w_mm / _MM_PER_INCH) * dpi
        h_px = (h_mm / _MM_PER_INCH) * dpi
        px = w_px * h_px
        if cap_px > 0:
            px = min(px, float(cap_px))
        peak = max(peak, px * 4.0 / (1024.0 * 1024.0))
    return peak


# Legibility / Limits floor: never clamp below the minimum DPI a render is allowed.
_MIN_DPI = 36


def _effective_dpi(
    dpi: int,
    page_sizes_mm: list[tuple[float, float]] | None,
    max_page_px: int,
) -> int:
    """DPI clamped so the LARGEST page in ``page_sizes_mm`` renders within
    ``max_page_px`` pixels.

    Returns ``dpi`` unchanged when page sizes are unknown, the budget is disabled
    (<= 0), or every page already fits. Only ever LOWERS the DPI, and floors at
    ``_MIN_DPI`` (the minimum ``Limits`` permits). Caveat: the floor can leave a
    maximally-sized page ABOVE the budget — pdfium caps the MediaBox at 14400pt,
    so a max page at 36 DPI is ~52 MP, which fits the DERIVED budget on any worker
    >= ~1.7 GB (incl. the 3-4 GB fleet: 96-100 MP budget) but NOT a smaller worker
    or a ``CLIPPYSHOT_MAX_PAGE_PX`` override below ~52 MP. In that regime 36 DPI is
    the best we can do (Limits forbids less); the caller logs the residual. This
    turns an oversized-page OOM into a valid, lower-resolution render."""
    if not page_sizes_mm or max_page_px <= 0:
        return dpi
    peak_px = 0.0
    for w_mm, h_mm in page_sizes_mm:
        w_px = (w_mm / _MM_PER_INCH) * dpi
        h_px = (h_mm / _MM_PER_INCH) * dpi
        peak_px = max(peak_px, w_px * h_px)
    if peak_px <= max_page_px:
        return dpi
    scale = math.sqrt(max_page_px / peak_px)
    return max(_MIN_DPI, int(dpi * scale))


@runtime_checkable
class Rasterizer(Protocol):
    name: str

    def rasterize(
        self,
        pdf_path: Path,
        out_dir: Path,
        dpi: int,
        max_pages: int,
        page_sizes_mm: list[tuple[float, float]] | None = None,
    ) -> list[RasterizedPage]: ...


class ShardingRasterizer(ABC):
    """Base for PDF rasterizers that render a page range by invoking an
    external CLI inside the sandbox, sharding the range across parallel
    invocations on a multi-CPU host.

    Concrete backends supply :meth:`_build_argv` (the per-range command) and,
    if their engine does not live under a system dir already exposed to the
    sandbox, :meth:`_extra_ro_mounts`. Everything else — sharding, output
    collection, zero-pad renaming, mm-dimension reporting — is shared so the
    two backends cannot drift on the parts downstream consumers depend on.
    """

    name: str = "sharding"

    def __init__(self, sandbox: Sandbox, rasterize_timeout_s: int = 60) -> None:
        self._sandbox = sandbox
        self._timeout = rasterize_timeout_s

    @abstractmethod
    def _build_argv(
        self, *, sandbox_pdf: Path, out_dir: Path, dpi: int, first: int, last: int
    ) -> list[str]:
        """Return the argv that renders pages [first, last] of ``sandbox_pdf``
        into ``out_dir`` as page-<absolute-index>.png at ``dpi``."""

    def _extra_ro_mounts(self) -> list[Mount]:
        """Read-only mounts the engine needs beyond the PDF input. Backends
        whose binary/libraries live outside the sandbox's curated system dirs
        (e.g. a venv-bundled shared library) override this to bind them in.
        Default: none."""
        return []

    def _env(self) -> dict[str, str]:
        """Extra environment for the render subprocess. Default: none."""
        return {}

    def _attach_apparmor(self) -> bool:
        """Whether to attach the soffice AppArmor profile to the render subprocess.
        Default True. A backend that runs a binary the soffice profile doesn't cover
        (e.g. pdfium reading its bundled libpdfium under sys.prefix) overrides to False."""
        return True

    def _shard_run(
        self,
        *,
        first: int,
        last: int,
        dpi: int,
        page_sizes_mm: list[tuple[float, float]] | None,
        budget_px: int,
        cpu_budget: int,
    ) -> list[tuple[int, int]]:
        """The page ranges one dpi-run should be rendered as. Plans, runs nothing.

        Shard count is bounded by the run's page count, the CPU budget, and the
        worker memory budget derived from the run's own largest page -- a run of
        oversized sheets costs more per concurrent render than a run of A4s.
        """
        run_pages = last - first + 1
        run_sizes = page_sizes_mm[first - 1:last] if page_sizes_mm else page_sizes_mm
        peak_mb = _max_page_peak_mb(run_sizes, dpi, cap_px=budget_px)
        mem_budget = max_concurrent_page_ops(per_page_peak_mb=peak_mb)
        shard_count = min(cpu_budget, mem_budget, run_pages)
        if run_pages < _MIN_PAGES_FOR_SHARDING or shard_count <= 1:
            return [(first, last)]
        per_shard = run_pages // shard_count
        ranges: list[tuple[int, int]] = []
        for index in range(shard_count):
            shard_first = first + index * per_shard
            shard_last = last if index == shard_count - 1 else first + (index + 1) * per_shard - 1
            ranges.append((shard_first, shard_last))
        return ranges

    def _run_range(
        self,
        *,
        sandbox_pdf: Path,
        out_dir: Path,
        dpi: int,
        first: int,
        last: int,
        pdf_parent: Path,
        page_sizes_mm: list[tuple[float, float]] | None = None,
        budget_px: int = 0,
    ) -> None:
        """Run a single render invocation over the [first, last] page range.

        The render DPI is clamped down if the largest page in THIS range would
        exceed ``budget_px`` pixels — so an oversized sheet renders (downscaled)
        instead of OOM-wedging a memory-capped guest. Per-range: a shard whose
        pages all fit the budget keeps full resolution; only shards containing an
        oversized page downscale.

        The caller splits pages into runs of equal clamped dpi before sharding
        (`_dpi_runs`), so an oversized page no longer shares an invocation with
        pages that fit -- which used to clamp a <4-page document entirely, and
        take whichever ordinary pages the even split put in the giant's shard
        down with it."""
        range_sizes = (
            page_sizes_mm[first - 1:last] if page_sizes_mm else None
        )
        eff_dpi = _effective_dpi(dpi, range_sizes, budget_px)
        if eff_dpi != dpi:
            _log.warning(
                "%s: clamped render DPI %d→%d for pages %d-%d "
                "(largest page exceeds %d-px budget)",
                self.name, dpi, eff_dpi, first, last, budget_px,
            )
            # Residual: the _MIN_DPI floor can leave a maximally-sized page still
            # over budget on a small/overridden worker (Limits forbids <36 DPI).
            # Surface it distinctly — the render proceeds best-effort but the OOM
            # guard is not fully honored in this (non-fleet) regime.
            if range_sizes and budget_px > 0:
                peak_px = max(
                    ((w / _MM_PER_INCH) * eff_dpi) * ((h / _MM_PER_INCH) * eff_dpi)
                    for w, h in range_sizes
                )
                if peak_px > budget_px:
                    _log.warning(
                        "%s: pages %d-%d still ~%.0f MP at the %d-DPI floor, over the "
                        "%.0f-MP budget — raise worker memory / CLIPPYSHOT_MAX_PAGE_PX",
                        self.name, first, last, peak_px / 1e6, eff_dpi, budget_px / 1e6,
                    )
        _assert_positional(sandbox_pdf)
        argv = self._build_argv(
            sandbox_pdf=sandbox_pdf, out_dir=_SANDBOX_OUT, dpi=eff_dpi, first=first, last=last
        )
        req = SandboxRequest(
            argv=argv,
            ro_mounts=[
                Mount(pdf_parent, _SANDBOX_IN, read_only=True),
                *self._extra_ro_mounts(),
            ],
            rw_mounts=[Mount(out_dir, _SANDBOX_OUT, read_only=False)],
            limits=Limits(
                timeout_s=self._timeout,
                max_pages=last - first + 1,
                dpi=eff_dpi,
            ),
            env=self._env(),
            attach_apparmor=self._attach_apparmor(),
        )
        result = self._sandbox.run(req)
        if result.killed or result.exit_code != 0:
            raise RasterizeError(
                f"{self.name} failed (exit={result.exit_code}, killed={result.killed}) "
                f"on pages {first}-{last}: "
                f"{result.stderr.decode(errors='replace')}"
            )

    def rasterize(
        self,
        pdf_path: Path,
        out_dir: Path,
        dpi: int,
        max_pages: int,
        page_sizes_mm: list[tuple[float, float]] | None = None,
    ) -> list[RasterizedPage]:
        # Resolve to absolute paths before deriving sandbox bind mounts: a
        # relative pdf_path would make pdf_path.parent resolve to "." (the cwd),
        # which we'd then bind-mount read-only into the sandbox.
        pdf_path = Path(pdf_path).resolve()
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        sandbox_pdf = _SANDBOX_IN / pdf_path.name

        # Shard the page range across parallel render invocations. Each engine
        # is single-threaded per page; the big win on a multi-CPU host is
        # launching N subprocesses each rendering a different range.
        #
        # Shard count is bounded by:
        #   - the page count (no point splitting 3 pages 4 ways)
        #   - CPUs, using half the host to leave room for the
        #     downstream per-page fan-out that runs right after
        #   - worker memory budget (see runtime.host_limits), which
        #     caps how many full-page RGB buffers can exist in RAM
        #     concurrently — otherwise a pathological spreadsheet
        #     render (one page can be 150MB+ uncompressed) can OOM
        #     the worker's cgroup.
        # Split on the CLAMPED dpi first, then shard within each run. The
        # budget is per PAGE but the dpi decision is per INVOCATION, so an
        # oversized page used to take every page sharing its invocation down to
        # its own reduced dpi. A document whose pages clamp alike -- nearly all
        # of them -- yields one run and the identical work as before.
        cpus = os.cpu_count() or 2
        cpu_budget = max(1, cpus // 2)
        budget_px = max_page_px()
        runs = _dpi_runs(page_sizes_mm, dpi, budget_px) or [(1, max_pages)]

        # PLAN every range first, then run them all through ONE bounded pool.
        # Running each dpi-run to completion before starting the next serialises
        # a document whose sizes alternate: it becomes many single-page runs, and
        # a 50-page mixed workbook would launch 50 rasterizers one after another
        # where the old code sharded them in parallel -- fast enough to miss the
        # worker deadline that the old code met.
        ranges: list[tuple[int, int]] = []
        for run_first, run_last in runs:
            if run_first > max_pages:
                break
            ranges.extend(self._shard_run(
                first=run_first, last=min(run_last, max_pages), dpi=dpi,
                page_sizes_mm=page_sizes_mm, budget_px=budget_px,
                cpu_budget=cpu_budget,
            ))

        # One global bound, from the whole document's clamped peak: concurrent
        # renders can come from different runs, so the memory ceiling has to
        # hold across all of them.
        doc_peak_mb = _max_page_peak_mb(page_sizes_mm, dpi, cap_px=budget_px)
        workers = min(
            cpu_budget, max_concurrent_page_ops(per_page_peak_mb=doc_peak_mb), len(ranges)
        )
        render = functools.partial(
            self._run_range,
            sandbox_pdf=sandbox_pdf, out_dir=out_dir, dpi=dpi,
            pdf_parent=pdf_path.parent, page_sizes_mm=page_sizes_mm,
            budget_px=budget_px,
        )
        if workers <= 1:
            for first, last in ranges:
                render(first=first, last=last)
        else:
            errors: list[Exception] = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [
                    ex.submit(render, first=first, last=last) for first, last in ranges
                ]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001 - reported after the pool drains
                        errors.append(e)
            if errors:
                raise errors[0]

        # Engines write page-1.png / page-01.png / ... (zero-padding varies by
        # engine and page count). Ignore derivative files like
        # page-001-focused.png that may already exist in the output dir.
        produced = sorted(
            src for src in out_dir.glob("page-*.png") if re.search(r"-(\d+)\.png$", src.name)
        )
        if not produced:
            raise RasterizeError(f"{self.name} produced no PNGs")

        # PDF page sizes (in mm) for the metadata. The caller can pass these
        # in to avoid re-opening the PDF (the converter already reads it for
        # the page-count + truncation decision); fall back to reading the PDF
        # ourselves if not provided.
        page_sizes = page_sizes_mm if page_sizes_mm is not None else self._page_sizes_mm(pdf_path)

        # Collapse to AT MOST ONE file per absolute page index BEFORE building pages.
        # Each shard writes page-<abs-index>.png (pypdfium2 zero-pads to the document's
        # page count, so indices are unique in the normal case). But two differently-named
        # files can parse to the SAME index — e.g. page-1.png + page-01.png, or a rare
        # stray/partial render artifact observed only under heavy host contention (16
        # concurrent runc workers). Left alone, that yields two RasterizedPages with the
        # same index, which fails host-side sealing with "duplicate artifact id" and loses
        # the WHOLE conversion. Deduplicate deterministically here: keep the largest file
        # for the index (a complete render dominates a partial), delete the rest. The
        # document's page count is fixed, so a second file for an existing index is the
        # anomaly to drop — not a distinct page.
        by_index: dict[int, Path] = {}
        dropped: list[str] = []
        for src in produced:
            idx = self._index_from_name(src.name)
            prev = by_index.get(idx)
            if prev is None:
                by_index[idx] = src
                continue
            keep, drop = (
                (src, prev) if src.stat().st_size > prev.stat().st_size else (prev, src)
            )
            by_index[idx] = keep
            dropped.append(drop.name)
            try:
                drop.unlink()
            except OSError:
                pass
        if dropped:
            _log.warning(
                "%s: collapsed %d duplicate-index page file(s) (kept largest per index): %s",
                self.name, len(dropped), dropped,
            )

        renamed: list[RasterizedPage] = []
        for idx in sorted(by_index):
            src = by_index[idx]
            new_name = f"page-{idx:03d}.png"
            dst = out_dir / new_name
            if src != dst:
                src.replace(dst)
            with Image.open(dst) as img:
                w_px, h_px = img.size
            w_mm, h_mm = page_sizes[idx - 1] if 0 <= idx - 1 < len(page_sizes) else (0.0, 0.0)
            renamed.append(
                RasterizedPage(
                    index=idx,
                    path=new_name,
                    width_px=w_px,
                    height_px=h_px,
                    width_mm=round(w_mm, 2),
                    height_mm=round(h_mm, 2),
                )
            )
        return renamed

    @staticmethod
    def _index_from_name(name: str) -> int:
        m = re.search(r"-(\d+)\.png$", name)
        if not m:
            raise RasterizeError(f"unexpected rasterizer filename: {name}")
        return int(m.group(1))

    @staticmethod
    def _page_sizes_mm(pdf: Path) -> list[tuple[float, float]]:
        reader = PdfReader(str(pdf))
        out: list[tuple[float, float]] = []
        for page in reader.pages:
            # A malformed PDF (derived from untrusted input via soffice) can
            # yield a missing/None mediabox; fall back to unknown (0,0) rather
            # than crash the whole conversion.
            try:
                box = page.mediabox
                w_pt = float(box.width)
                h_pt = float(box.height)
                w_mm = (w_pt / _PT_PER_INCH) * _MM_PER_INCH
                h_mm = (h_pt / _PT_PER_INCH) * _MM_PER_INCH
            except (AttributeError, TypeError, ValueError):
                w_mm, h_mm = 0.0, 0.0
            out.append((w_mm, h_mm))
        return out
