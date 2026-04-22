"""Integration: direct QR and OCR on fixture images.

Runs the scanner binaries (ZXingReader, tesseract) on the host and
verifies they decode the fixtures correctly. This is a stepping stone
toward the full sandbox-backed smoketest (T19 corpus run).
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.integration


def test_qr_scan_on_host(qr_fixture_png):
    from clippyshot.qr import scan_qr
    results = scan_qr(qr_fixture_png, timeout_s=15)
    assert len(results) >= 1, "zxing-cpp failed to decode the fixture QR"
    values = [r.value for r in results]
    assert "Hello QR" in values, f"expected 'Hello QR' in {values}"
    assert any(r.format == "qr_code" for r in results)


def test_ocr_scan_on_host(text_fixture_png):
    from clippyshot.ocr import run_ocr
    out = run_ocr(text_fixture_png, timeout_s=30)
    text = out.text or ""
    assert "Hello" in text, f"OCR output missing 'Hello': {text!r}"
    assert "OCR" in text, f"OCR output missing 'OCR': {text!r}"


def test_qr_scan_empty_png(tmp_path):
    """A PNG with no QR returns empty list, not an error."""
    from PIL import Image
    from clippyshot.qr import scan_qr
    png = tmp_path / "blank.png"
    Image.new("RGB", (100, 100), "white").save(png)
    results = scan_qr(png, timeout_s=15)
    assert results == []
