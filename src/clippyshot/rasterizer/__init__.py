"""PDF-to-PNG rasterizer abstraction."""
from clippyshot.rasterizer.base import Rasterizer
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer

__all__ = ["Rasterizer", "PdftoppmRasterizer"]
