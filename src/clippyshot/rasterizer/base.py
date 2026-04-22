"""Rasterizer protocol."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from clippyshot.types import RasterizedPage


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
