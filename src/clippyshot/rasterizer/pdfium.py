"""PDFium-backed rasterizer (via the pypdfium2 CLI).

PDFium is Chrome's PDF engine: permissively (BSD) licensed, continuously
fuzzed by Google, and — unlike MuPDF — it clamps oversized pages to the same
14400pt MediaBox ceiling poppler does, so giant SinglePageSheets spreadsheets
render at identical dimensions instead of blowing past a pixmap cap.

pypdfium2 bundles libpdfium.so inside the Python venv rather than under a
system dir, so the render subprocess needs the venv bind-mounted into the
sandbox (a no-op under the container backend, which already sees the full
rootfs). PDFium is not thread-safe, but each shard is its own process, so the
existing per-range sharding model is exactly the right shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

from clippyshot.rasterizer.base import _PT_PER_INCH, ShardingRasterizer
from clippyshot.sandbox.base import Mount, Sandbox

# The venv that ships pypdfium2 (and its bundled libpdfium.so). Under the
# deploy image this is /opt/clippyshot; whatever interpreter is running
# clippyshot is by construction the one with pypdfium2 installed.
_VENV_ROOT = Path(sys.prefix)
_DEFAULT_PYPDFIUM2 = str(_VENV_ROOT / "bin" / "pypdfium2")


class PdfiumRasterizer(ShardingRasterizer):
    name = "pdfium"

    def __init__(
        self,
        sandbox: Sandbox,
        pypdfium2_path: str = _DEFAULT_PYPDFIUM2,
        rasterize_timeout_s: int = 60,
        venv_root: Path = _VENV_ROOT,
    ) -> None:
        super().__init__(sandbox, rasterize_timeout_s)
        self._pypdfium2 = pypdfium2_path
        # Resolve symlinks: a venv's sys.prefix is often a symlink, and
        # bwrap/nsjail bind mounts want the real path.
        self._venv_root = Path(venv_root).resolve()

    def _extra_ro_mounts(self) -> list[Mount]:
        # bwrap/nsjail curate the rootfs and only expose system dirs; the
        # pypdfium2 console script + bundled libpdfium.so live under the venv,
        # so bind it in read-only (identity path so the shebang + dlopen
        # resolve). Under the container backend this maps to itself and is
        # a harmless no-op.
        return [Mount(self._venv_root, self._venv_root, read_only=True)]

    def _env(self) -> dict[str, str]:
        # pypdfium2's CLI defaults to debug-level logging on stderr; quiet it
        # so a 50-page sharded render doesn't spew per-page noise.
        return {"PYPDFIUM_LOGLEVEL": "error"}

    def _build_argv(
        self, *, sandbox_pdf: Path, out_dir: Path, dpi: int, first: int, last: int
    ) -> list[str]:
        # pypdfium2 uses a DPI-independent --scale (1.0 == 72 DPI) and writes
        # page-<absolute-index>.png (zero-padded to the document's page count),
        # so sharded ranges never collide. "--" terminates option parsing
        # before the positional PDF path.
        scale = dpi / _PT_PER_INCH
        return [
            self._pypdfium2,
            "render",
            "--pages", f"{first}-{last}",
            "--scale", f"{scale:.6f}",
            "--format", "png",
            "--output", str(out_dir),
            "--prefix", "page-",
            # Render serially WITHIN this invocation. The outer ShardingRasterizer already
            # parallelises across page-range subprocesses; pypdfium2's own default multiprocessing
            # would multiply that — each internal worker holds a full-page RGBA buffer, so on an
            # oversized page (~2.7 GB each) the internal fan-out is a host-memory-exhaustion
            # multiplier (measured ~+425% RSS). One render per shard keeps the aggregate bounded
            # by the size-aware shard_count.
            "--processes", "1",
            "--", str(sandbox_pdf),
        ]
