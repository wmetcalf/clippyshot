"""poppler pdftoppm-backed rasterizer."""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from clippyshot.errors import RasterizeError
from clippyshot.limits import Limits
from clippyshot.runtime.host_limits import max_concurrent_page_ops
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.types import RasterizedPage


_PT_PER_INCH = 72.0
_MM_PER_INCH = 25.4
# Page-count threshold below which sharding adds more subprocess
# overhead than it saves.
_MIN_PAGES_FOR_SHARDING = 4


class PdftoppmRasterizer:
    name = "pdftoppm"

    def __init__(
        self,
        sandbox: Sandbox,
        pdftoppm_path: str = "/usr/bin/pdftoppm",
        rasterize_timeout_s: int = 60,
    ) -> None:
        self._sandbox = sandbox
        self._pdftoppm = pdftoppm_path
        self._timeout = rasterize_timeout_s

    def _run_pdftoppm(
        self,
        *,
        sandbox_pdf: Path,
        out_dir: Path,
        dpi: int,
        first: int,
        last: int,
        pdf_parent: Path,
    ) -> None:
        """Run a single pdftoppm invocation over the [first, last] page range."""
        argv = [
            self._pdftoppm,
            "-png",
            "-r", str(dpi),
            "-f", str(first),
            "-l", str(last),
            str(sandbox_pdf),
            "/sandbox/out/page",
        ]
        req = SandboxRequest(
            argv=argv,
            ro_mounts=[Mount(pdf_parent, Path("/sandbox/in"), read_only=True)],
            rw_mounts=[Mount(out_dir, Path("/sandbox/out"), read_only=False)],
            limits=Limits(
                timeout_s=self._timeout,
                max_pages=last - first + 1,
                dpi=dpi,
            ),
        )
        result = self._sandbox.run(req)
        if result.killed or result.exit_code != 0:
            raise RasterizeError(
                f"pdftoppm failed (exit={result.exit_code}, killed={result.killed}) "
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
        pdf_path = Path(pdf_path)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        sandbox_pdf = Path("/sandbox/in") / pdf_path.name

        # Shard the page range across parallel pdftoppm invocations.
        # pdftoppm itself is single-threaded per page; the big win on a
        # multi-CPU host is launching N subprocesses each rendering a
        # different range.
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
        cpus = os.cpu_count() or 2
        cpu_budget = max(1, cpus // 2)
        mem_budget = max_concurrent_page_ops()
        shard_count = min(cpu_budget, mem_budget, max_pages)
        if max_pages < _MIN_PAGES_FOR_SHARDING or shard_count <= 1:
            # Single-shot fast path: one pdftoppm for the whole range.
            self._run_pdftoppm(
                sandbox_pdf=sandbox_pdf, out_dir=out_dir, dpi=dpi,
                first=1, last=max_pages, pdf_parent=pdf_path.parent,
            )
        else:
            # Even split; last shard absorbs the remainder.
            per_shard = max_pages // shard_count
            ranges: list[tuple[int, int]] = []
            for i in range(shard_count):
                first = i * per_shard + 1
                last = max_pages if i == shard_count - 1 else (i + 1) * per_shard
                ranges.append((first, last))

            errors: list[Exception] = []
            with ThreadPoolExecutor(max_workers=shard_count) as ex:
                futures = [
                    ex.submit(
                        self._run_pdftoppm,
                        sandbox_pdf=sandbox_pdf, out_dir=out_dir, dpi=dpi,
                        first=first, last=last, pdf_parent=pdf_path.parent,
                    )
                    for first, last in ranges
                ]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        errors.append(e)
            if errors:
                raise errors[0]

        # pdftoppm writes page-1.png, page-2.png, ... Ignore derivative
        # files like page-001-focused.png that may already exist in the output dir.
        produced = sorted(
            src for src in out_dir.glob("page-*.png") if re.search(r"-(\d+)\.png$", src.name)
        )
        if not produced:
            raise RasterizeError("pdftoppm produced no PNGs")

        # PDF page sizes (in mm) for the metadata. The caller can pass these
        # in to avoid re-opening the PDF (the converter already reads it for
        # the page-count + truncation decision); fall back to reading the PDF
        # ourselves if not provided.
        page_sizes = page_sizes_mm if page_sizes_mm is not None else self._page_sizes_mm(pdf_path)

        renamed: list[RasterizedPage] = []
        for src in produced:
            idx = self._index_from_name(src.name)
            new_name = f"page-{idx:03d}.png"
            dst = out_dir / new_name
            if src != dst:
                src.replace(dst)
            with Image.open(dst) as img:
                w_px, h_px = img.size
            w_mm, h_mm = page_sizes[idx - 1] if idx - 1 < len(page_sizes) else (0.0, 0.0)
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
        renamed.sort(key=lambda p: p.index)
        return renamed

    @staticmethod
    def _index_from_name(name: str) -> int:
        m = re.search(r"-(\d+)\.png$", name)
        if not m:
            raise RasterizeError(f"unexpected pdftoppm filename: {name}")
        return int(m.group(1))

    @staticmethod
    def _page_sizes_mm(pdf: Path) -> list[tuple[float, float]]:
        reader = PdfReader(str(pdf))
        out: list[tuple[float, float]] = []
        for page in reader.pages:
            box = page.mediabox
            w_pt = float(box.width)
            h_pt = float(box.height)
            w_mm = (w_pt / _PT_PER_INCH) * _MM_PER_INCH
            h_mm = (h_pt / _PT_PER_INCH) * _MM_PER_INCH
            out.append((w_mm, h_mm))
        return out
