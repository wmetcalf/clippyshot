"""Every rendered page must always have both `qr` and `ocr` keys,
regardless of whether the scanners were enabled."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

from clippyshot.converter import ConvertOptions, Converter
from clippyshot.limits import Limits
from clippyshot.types import DetectedType, RasterizedPage


def _make_stub_png() -> bytes:
    """Create a small non-blank PNG using Pillow (a 2x2 checkerboard)."""
    from PIL import Image as _Image
    img = _Image.new("RGB", (2, 2), (255, 0, 0))
    img.putpixel((1, 0), (0, 0, 255))
    img.putpixel((0, 1), (0, 255, 0))
    img.putpixel((1, 1), (255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_stub_converter(tmp_path: Path):
    """Build a Converter with fully-stubbed dependencies so convert() runs
    end-to-end without touching soffice, pdftoppm, zxing, or tesseract."""
    _TINY_PNG = _make_stub_png()

    detector = MagicMock()
    detector.detect.return_value = DetectedType(
        label="docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extension_hint="docx",
        confidence=0.99,
        source="magika",
        agreed_with_extension=True,
        warnings=[],
    )

    runner = MagicMock()

    def fake_convert_to_pdf(input_path, pdf_dir, limits, label):
        from PIL import Image
        pdf = Path(pdf_dir) / "x.pdf"
        # Use Pillow to generate a real one-page PDF so PdfReader can open it.
        img = Image.new("RGB", (612, 792), (255, 255, 255))
        img.save(str(pdf), "PDF", resolution=72.0)
        return pdf

    runner.convert_to_pdf.side_effect = fake_convert_to_pdf

    rasterizer = MagicMock()
    rasterizer.name = "pdftoppm"

    def fake_rasterize(pdf_path, output_dir, *, dpi, max_pages, page_sizes_mm):
        png_path = output_dir / "page-001.png"
        png_path.write_bytes(_TINY_PNG)
        return [RasterizedPage(
            index=1, path="page-001.png",
            width_px=2, height_px=2,
            width_mm=0.17, height_mm=0.17,
        )]

    rasterizer.rasterize.side_effect = fake_rasterize

    conv = Converter(
        detector=detector,
        runner=runner,
        rasterizer=rasterizer,
        sandbox_backend="container",
    )
    return conv


def test_per_page_schema_qr_ocr_always_present_disabled(tmp_path):
    conv = _make_stub_converter(tmp_path)
    input_path = tmp_path / "x.docx"
    input_path.write_bytes(b"fake")
    output_dir = tmp_path / "out"
    result = conv.convert(
        input_path, output_dir,
        ConvertOptions(limits=Limits(), qr_enabled=False, ocr_enabled=False),
    )
    for page in result.metadata["pages"]:
        assert "qr" in page and page["qr"] == []
        assert "ocr" in page
        assert page["ocr"]["skipped"] == "disabled"
        assert page["ocr"]["text"] == ""

    assert result.metadata["render"]["scanners"]["qr"]["enabled"] is False
    assert result.metadata["render"]["scanners"]["ocr"]["enabled"] is False


def test_per_page_schema_qr_ocr_always_present_enabled_but_scan_fails(tmp_path):
    """Even when the scanner binaries fail (or don't run due to blank page),
    the pipeline produces a valid result with consistent schema."""
    conv = _make_stub_converter(tmp_path)
    input_path = tmp_path / "x.docx"
    input_path.write_bytes(b"fake")
    output_dir = tmp_path / "out"
    result = conv.convert(
        input_path, output_dir,
        ConvertOptions(limits=Limits(), qr_enabled=True, ocr_enabled=True),
    )
    for page in result.metadata["pages"]:
        assert "qr" in page
        assert isinstance(page["qr"], list)
        assert "ocr" in page
        assert isinstance(page["ocr"]["text"], str)


def test_per_page_has_image_count(tmp_path):
    """The per-page record emits image_count (int >= 0), not has_images (bool).
    The render block exposes image_page_count and total_image_count aggregates."""
    conv = _make_stub_converter(tmp_path)
    input_path = tmp_path / "x.docx"
    input_path.write_bytes(b"fake")
    output_dir = tmp_path / "out2"
    result = conv.convert(input_path, output_dir, ConvertOptions(limits=Limits()))
    for page in result.metadata["pages"]:
        assert "image_count" in page, "per-page image_count missing"
        assert "has_images" not in page, "legacy has_images should be gone"
        assert isinstance(page["image_count"], int)
        assert page["image_count"] >= 0
    r = result.metadata["render"]
    assert "image_page_count" in r
    assert "total_image_count" in r
    assert isinstance(r["image_page_count"], int)
    assert isinstance(r["total_image_count"], int)
    assert r["image_page_count"] >= 0
    assert r["total_image_count"] >= 0
