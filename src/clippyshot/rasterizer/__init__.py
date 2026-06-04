"""PDF-to-PNG rasterizer abstraction."""
from __future__ import annotations

from clippyshot.limits import Limits
from clippyshot.rasterizer.base import Rasterizer, ShardingRasterizer
from clippyshot.rasterizer.pdfium import PdfiumRasterizer
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
from clippyshot.sandbox.base import Sandbox

__all__ = [
    "Rasterizer",
    "ShardingRasterizer",
    "PdftoppmRasterizer",
    "PdfiumRasterizer",
    "build_rasterizer",
]


def build_rasterizer(
    sandbox: Sandbox,
    name: str | None = None,
    *,
    rasterize_timeout_s: int = 60,
) -> Rasterizer:
    """Construct the configured rasterizer backend.

    The choice is a deploy-time decision (the converter builds one rasterizer
    at startup), so it funnels through ``Limits.from_env().rasterizer`` like
    every other tunable. ``CLIPPYSHOT_RASTERIZER`` selects ``pdfium`` (default,
    ~2x faster than poppler and clamps giant pages identically) or
    ``pdftoppm`` (poppler, the long-standing fallback).
    """
    if name is None:
        name = Limits.from_env().rasterizer
    if name == "pdfium":
        return PdfiumRasterizer(sandbox=sandbox, rasterize_timeout_s=rasterize_timeout_s)
    if name == "pdftoppm":
        return PdftoppmRasterizer(sandbox=sandbox, rasterize_timeout_s=rasterize_timeout_s)
    raise ValueError(
        f"unknown rasterizer {name!r} (expected 'pdfium' or 'pdftoppm')"
    )
