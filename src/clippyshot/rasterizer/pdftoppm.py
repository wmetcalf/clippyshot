"""poppler pdftoppm-backed rasterizer."""
from __future__ import annotations

from pathlib import Path

from clippyshot.rasterizer.base import ShardingRasterizer
from clippyshot.sandbox.base import Sandbox


class PdftoppmRasterizer(ShardingRasterizer):
    name = "pdftoppm"

    def __init__(
        self,
        sandbox: Sandbox,
        pdftoppm_path: str = "/usr/bin/pdftoppm",
        rasterize_timeout_s: int = 60,
    ) -> None:
        super().__init__(sandbox, rasterize_timeout_s)
        self._pdftoppm = pdftoppm_path

    def _build_argv(
        self, *, sandbox_pdf: Path, out_dir: Path, dpi: int, first: int, last: int
    ) -> list[str]:
        # pdftoppm takes the input as a bare positional and writes
        # <prefix>-<page>.png; the prefix here is /sandbox/out/page.
        return [
            self._pdftoppm,
            "-png",
            "-r", str(dpi),
            "-f", str(first),
            "-l", str(last),
            str(sandbox_pdf),
            str(out_dir / "page"),
        ]
