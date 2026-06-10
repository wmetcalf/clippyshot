from pathlib import Path

import pytest

from clippyshot.rasterizer import build_rasterizer
from clippyshot.rasterizer.pdfium import PdfiumRasterizer
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
from clippyshot.sandbox.base import Mount, SandboxRequest
from clippyshot.types import RasterizedPage, SandboxResult

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "safe" / "two_page.pdf"


# A 1x1 transparent PNG (smallest valid PNG).
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


class FakeSandbox:
    name = "fake"

    def __init__(self) -> None:
        self.last_request: SandboxRequest | None = None

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.last_request = request
        out_host = next(
            m.host_path for m in request.rw_mounts if m.sandbox_path == Path("/sandbox/out")
        )
        # pdftoppm -png writes page-1.png, page-2.png, ... (no zero padding by default)
        (out_host / "page-1.png").write_bytes(_TINY_PNG)
        (out_host / "page-2.png").write_bytes(_TINY_PNG)
        return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=10, killed=False)

    def smoketest(self) -> SandboxResult:
        return SandboxResult(0, b"", b"", 1, False)


def test_rasterize_with_fake_sandbox_returns_pages(tmp_path: Path):
    sb = FakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    r = PdftoppmRasterizer(sandbox=sb)
    pages = r.rasterize(FIXTURE, out, dpi=150, max_pages=10)
    assert len(pages) == 2
    assert pages[0].index == 1
    assert pages[1].index == 2
    assert all(isinstance(p, RasterizedPage) for p in pages)
    # Pages should be renamed to zero-padded form on disk.
    assert (out / "page-001.png").exists()
    assert (out / "page-002.png").exists()


def test_rasterize_passes_dpi_to_sandbox(tmp_path: Path):
    sb = FakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    PdftoppmRasterizer(sandbox=sb).rasterize(FIXTURE, out, dpi=200, max_pages=5)
    argv = sb.last_request.argv
    assert "-r" in argv
    assert argv[argv.index("-r") + 1] == "200"
    assert "-l" in argv
    assert argv[argv.index("-l") + 1] == "5"


def test_rasterize_reports_mm_dimensions_from_pdf_mediabox(tmp_path: Path):
    sb = FakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    pages = PdftoppmRasterizer(sandbox=sb).rasterize(FIXTURE, out, dpi=150, max_pages=10)
    # Letter (612x792 pt) → ~215.9 x 279.4 mm
    assert 215 < pages[0].width_mm < 217
    assert 279 < pages[0].height_mm < 280


def test_rasterize_propagates_sandbox_failure(tmp_path: Path):
    from clippyshot.errors import RasterizeError

    class BadSandbox:
        name = "bad"

        def run(self, request: SandboxRequest) -> SandboxResult:
            return SandboxResult(
                exit_code=1, stdout=b"", stderr=b"pdftoppm failed",
                duration_ms=5, killed=False,
            )

        def smoketest(self) -> SandboxResult:
            return SandboxResult(0, b"", b"", 1, False)

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RasterizeError):
        PdftoppmRasterizer(sandbox=BadSandbox()).rasterize(
            FIXTURE, out, dpi=150, max_pages=10
        )


def test_rasterize_raises_when_no_pngs_produced(tmp_path: Path):
    from clippyshot.errors import RasterizeError

    class EmptySandbox:
        name = "empty"

        def run(self, request: SandboxRequest) -> SandboxResult:
            return SandboxResult(0, b"", b"", 1, False)

        def smoketest(self) -> SandboxResult:
            return SandboxResult(0, b"", b"", 1, False)

    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RasterizeError):
        PdftoppmRasterizer(sandbox=EmptySandbox()).rasterize(
            FIXTURE, out, dpi=150, max_pages=10
        )


def test_rasterize_ignores_derivative_pngs_with_non_numeric_suffixes(tmp_path: Path):
    class MixedOutputSandbox(FakeSandbox):
        def run(self, req):
            out_host = next(m.host_path for m in req.rw_mounts if m.sandbox_path == Path("/sandbox/out"))
            (out_host / "page-1.png").write_bytes(_TINY_PNG)
            (out_host / "page-001-focused.png").write_bytes(_TINY_PNG)
            return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1, killed=False)

    r = PdftoppmRasterizer(sandbox=MixedOutputSandbox())
    pages = r.rasterize(FIXTURE, tmp_path / "out", dpi=150, max_pages=10)

    assert [page.index for page in pages] == [1]
    assert [page.path for page in pages] == ["page-001.png"]
    assert (tmp_path / "out" / "page-001-focused.png").exists()


def test_rasterize_dedupes_duplicate_index_keeping_largest(tmp_path: Path):
    """Two files parsing to the SAME page index (page-1.png + page-01.png, or a rare
    stray/partial render artifact seen only under heavy host contention) must collapse
    to ONE page per index — otherwise host-side sealing fails with 'duplicate artifact
    id' and loses the whole conversion. The larger (complete) file is kept; the stray
    is deleted."""
    import io

    from PIL import Image as _Image

    buf = io.BytesIO()
    _Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    big_png = buf.getvalue()
    assert len(big_png) > len(_TINY_PNG)

    class DupIndexSandbox(FakeSandbox):
        def run(self, req):
            out_host = next(
                m.host_path for m in req.rw_mounts if m.sandbox_path == Path("/sandbox/out")
            )
            (out_host / "page-01.png").write_bytes(_TINY_PNG)   # small, parses to idx 1
            (out_host / "page-1.png").write_bytes(big_png)      # large, idx 1 (the keeper)
            (out_host / "page-2.png").write_bytes(big_png)      # idx 2
            return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=1, killed=False)

    r = PdftoppmRasterizer(sandbox=DupIndexSandbox())
    pages = r.rasterize(FIXTURE, tmp_path / "out", dpi=150, max_pages=10)

    assert [p.index for p in pages] == [1, 2]               # exactly one page per index
    out = tmp_path / "out"
    assert (out / "page-001.png").read_bytes() == big_png   # kept the larger of the idx-1 pair
    assert not (out / "page-1.png").exists()                # stray collapsed away
    assert not (out / "page-01.png").exists()


# --- PDFium backend -------------------------------------------------------


class PdfiumFakeSandbox(FakeSandbox):
    """pypdfium2 writes zero-padded page-NN.png (padding from the doc's page
    count), unlike pdftoppm's page-N.png — exercise that the shared collection
    logic normalizes both to page-NNN.png."""

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.last_request = request
        out_host = next(
            m.host_path for m in request.rw_mounts if m.sandbox_path == Path("/sandbox/out")
        )
        (out_host / "page-01.png").write_bytes(_TINY_PNG)
        (out_host / "page-02.png").write_bytes(_TINY_PNG)
        return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=10, killed=False)


def test_pdfium_renders_and_normalizes_zero_padded_names(tmp_path: Path):
    sb = PdfiumFakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    pages = PdfiumRasterizer(sandbox=sb).rasterize(FIXTURE, out, dpi=150, max_pages=10)
    assert [p.index for p in pages] == [1, 2]
    assert (out / "page-001.png").exists()
    assert (out / "page-002.png").exists()


def test_pdfium_builds_render_argv_with_scale_and_pages(tmp_path: Path):
    sb = PdfiumFakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    PdfiumRasterizer(sandbox=sb).rasterize(FIXTURE, out, dpi=144, max_pages=3)
    argv = sb.last_request.argv
    assert argv[1] == "render"
    # 144 DPI -> scale 2.0 (144/72)
    assert "--scale" in argv
    assert float(argv[argv.index("--scale") + 1]) == pytest.approx(2.0)
    assert "--pages" in argv
    assert "--format" in argv and argv[argv.index("--format") + 1] == "png"
    assert argv[argv.index("--prefix") + 1] == "page-"
    # the positional PDF path is terminated by "--"
    assert argv[-2] == "--"
    assert argv[-1].endswith(".pdf")


def test_pdfium_mounts_the_venv_readonly(tmp_path: Path):
    sb = PdfiumFakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    venv = Path("/opt/somewhere")
    PdfiumRasterizer(sandbox=sb, venv_root=venv).rasterize(FIXTURE, out, dpi=150, max_pages=2)
    ro = sb.last_request.ro_mounts
    # the PDF input mount plus an identity, read-only venv mount
    assert Mount(venv, venv, read_only=True) in ro


def test_pdfium_quiets_logging_via_env(tmp_path: Path):
    sb = PdfiumFakeSandbox()
    out = tmp_path / "out"
    out.mkdir()
    PdfiumRasterizer(sandbox=sb).rasterize(FIXTURE, out, dpi=150, max_pages=2)
    assert sb.last_request.env.get("PYPDFIUM_LOGLEVEL") == "error"


# --- factory --------------------------------------------------------------


def test_build_rasterizer_selects_backend_by_name():
    sb = FakeSandbox()
    assert isinstance(build_rasterizer(sb, "pdfium"), PdfiumRasterizer)
    assert isinstance(build_rasterizer(sb, "pdftoppm"), PdftoppmRasterizer)


def test_build_rasterizer_defaults_to_pdfium(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_RASTERIZER", raising=False)
    assert isinstance(build_rasterizer(FakeSandbox()), PdfiumRasterizer)


def test_build_rasterizer_honors_env(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_RASTERIZER", "pdftoppm")
    assert isinstance(build_rasterizer(FakeSandbox()), PdftoppmRasterizer)


def test_build_rasterizer_rejects_unknown():
    with pytest.raises(ValueError):
        build_rasterizer(FakeSandbox(), "ghostscript")
