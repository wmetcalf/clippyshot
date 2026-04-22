"""poppler pdftoppm-backed rasterizer."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from clippyshot.errors import RasterizeError
from clippyshot.limits import Limits
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.types import RasterizedPage


_PT_PER_INCH = 72.0
_MM_PER_INCH = 25.4


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
        argv = [
            self._pdftoppm,
            "-png",
            "-r", str(dpi),
            "-f", "1",
            "-l", str(max_pages),
            str(sandbox_pdf),
            "/sandbox/out/page",
        ]
        req = SandboxRequest(
            argv=argv,
            ro_mounts=[Mount(pdf_path.parent, Path("/sandbox/in"), read_only=True)],
            rw_mounts=[Mount(out_dir, Path("/sandbox/out"), read_only=False)],
            limits=Limits(timeout_s=self._timeout, max_pages=max_pages, dpi=dpi),
        )
        result = self._sandbox.run(req)
        if result.killed or result.exit_code != 0:
            raise RasterizeError(
                f"pdftoppm failed (exit={result.exit_code}, killed={result.killed}): "
                f"{result.stderr.decode(errors='replace')}"
            )

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
