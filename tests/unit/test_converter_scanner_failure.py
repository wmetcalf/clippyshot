"""Verify a QR/OCR scanner exception does not fail the conversion."""
from __future__ import annotations

from pathlib import Path

from clippyshot.converter import _process_page_scanners
from clippyshot.qr import ScanError
from clippyshot.ocr import OCRError


def test_qr_error_does_not_raise(tmp_path):
    png = tmp_path / "page-001.png"
    png.write_bytes(b"fake")
    rec = {"index": 1, "file": "page-001.png"}

    def fake_scan_qr(path, **kwargs):
        raise ScanError("mock zxing exit 2")

    def fake_run_ocr(path, **kwargs):
        raise AssertionError("ocr should not have run, disabled")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path,
        rec,
        is_blank=False,
        qr_enabled=True,
        qr_formats="qr_code",
        qr_timeout_s=10,
        ocr_enabled=False,
        ocr_lang="eng",
        ocr_psm=6,
        ocr_time_left=lambda: 60.0,
        _qr_fn=fake_scan_qr,
        _ocr_fn=fake_run_ocr,
    )
    assert qr_list == []
    assert qr_skipped == "error"
    assert ocr_obj == {"text": "", "char_count": 0, "duration_ms": 0, "skipped": "disabled"}
    assert any(w["code"] == "qr_scan_error" and w["page"] == 1 for w in warnings)
    assert "mock zxing exit 2" in warnings[0]["message"]


def test_ocr_error_does_not_raise(tmp_path):
    png = tmp_path / "page-001.png"
    png.write_bytes(b"fake")
    rec = {"index": 1, "file": "page-001.png"}

    def fake_scan_qr(path, **kwargs):
        return []

    def fake_run_ocr(path, **kwargs):
        raise OCRError("tesseract exited 1: corrupt image")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path,
        rec,
        is_blank=False,
        qr_enabled=True,
        qr_formats="qr_code",
        qr_timeout_s=10,
        ocr_enabled=True,
        ocr_lang="eng",
        ocr_psm=6,
        ocr_time_left=lambda: 60.0,
        _qr_fn=fake_scan_qr,
        _ocr_fn=fake_run_ocr,
    )
    assert qr_list == []
    assert qr_skipped is None
    assert ocr_obj["text"] == ""
    assert ocr_obj["skipped"] == "error"
    assert any(w["code"] == "ocr_scan_error" and w["page"] == 1 for w in warnings)


def test_blank_page_skips_both_scanners(tmp_path):
    png = tmp_path / "page-001.png"
    png.write_bytes(b"fake")
    rec = {"index": 1, "file": "page-001.png"}

    def fail(*args, **kwargs):
        raise AssertionError("scanner should not have run on blank page")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path,
        rec,
        is_blank=True,
        qr_enabled=True,
        qr_formats="qr_code",
        qr_timeout_s=10,
        ocr_enabled=True,
        ocr_lang="eng",
        ocr_psm=6,
        ocr_time_left=lambda: 60.0,
        _qr_fn=fail,
        _ocr_fn=fail,
    )
    assert qr_list == []
    assert qr_skipped == "blank_page"
    assert ocr_obj["skipped"] == "blank_page"
    assert warnings == []


def test_disabled_scanners(tmp_path):
    png = tmp_path / "page-001.png"
    png.write_bytes(b"fake")
    rec = {"index": 1, "file": "page-001.png"}

    def fail(*args, **kwargs):
        raise AssertionError("scanner should not have run, disabled")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=False, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        _qr_fn=fail, _ocr_fn=fail,
    )
    assert qr_list == []
    assert qr_skipped == "disabled"
    assert ocr_obj["skipped"] == "disabled"
    assert warnings == []


def test_happy_path_both_scanners(tmp_path):
    from clippyshot.qr import QRResult
    from clippyshot.ocr import OCRResult
    png = tmp_path / "page-001.png"
    png.write_bytes(b"fake")
    rec = {"index": 1, "file": "page-001.png"}

    def fake_scan_qr(path, **kwargs):
        return [QRResult(format="qr_code", value="https://example.com",
                         position="10,10 20,20 30,30 40,40",
                         error_correction_level="L", is_mirrored=False,
                         raw_bytes_hex=None)]

    def fake_run_ocr(path, **kwargs):
        return OCRResult(text="hello world", char_count=11, duration_ms=42)

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=True, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        _qr_fn=fake_scan_qr, _ocr_fn=fake_run_ocr,
    )
    assert len(qr_list) == 1
    assert qr_list[0]["value"] == "https://example.com"
    assert qr_list[0]["format"] == "qr_code"
    assert qr_skipped is None
    assert ocr_obj == {"text": "hello world", "char_count": 11, "duration_ms": 42}
    assert warnings == []


def test_ocr_skipped_when_budget_exhausted(tmp_path):
    """When ocr_time_left() returns 0, OCR is skipped with timeout_budget."""
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fail(*args, **kwargs):
        raise AssertionError("OCR should not run when budget exhausted")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6,
        ocr_time_left=lambda: 0.0,     # budget gone
        has_images=True, ocr_all=False,
        _qr_fn=lambda *a, **k: [], _ocr_fn=fail,
    )
    assert ocr_obj["skipped"] == "timeout_budget"
