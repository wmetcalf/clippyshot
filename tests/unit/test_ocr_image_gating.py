"""Tests for the ocr_enabled + ocr_all + has_images gating logic."""
from __future__ import annotations

import pytest

from clippyshot.converter import _process_page_scanners


def test_ocr_skipped_when_no_images(tmp_path):
    """ocr_enabled=True, ocr_all=False, has_images=False → skipped='no_images'"""
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fake_ocr(path, **kwargs):
        raise AssertionError("OCR should not have been called for a text-only page")

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        has_images=False, ocr_all=False,
        _qr_fn=lambda *a, **k: [], _ocr_fn=fake_ocr,
    )
    assert ocr_obj["skipped"] == "no_images"


def test_ocr_runs_when_has_images_true(tmp_path):
    """ocr_enabled=True, ocr_all=False, has_images=True → OCR runs normally."""
    from clippyshot.ocr import OCRResult
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fake_ocr(path, **kwargs):
        return OCRResult(text="x", char_count=1, duration_ms=1)

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        has_images=True, ocr_all=False,
        _qr_fn=lambda *a, **k: [], _ocr_fn=fake_ocr,
    )
    assert ocr_obj["text"] == "x"
    assert "skipped" not in ocr_obj


def test_ocr_all_override_runs_on_text_only_page(tmp_path):
    """ocr_enabled=True, ocr_all=True, has_images=False → OCR runs (override)."""
    from clippyshot.ocr import OCRResult
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fake_ocr(path, **kwargs):
        return OCRResult(text="y", char_count=1, duration_ms=1)

    qr_list, qr_skipped, ocr_obj, warnings = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        has_images=False, ocr_all=True,  # override
        _qr_fn=lambda *a, **k: [], _ocr_fn=fake_ocr,
    )
    assert ocr_obj["text"] == "y"
    assert "skipped" not in ocr_obj


def test_ocr_disabled_takes_priority_over_no_images(tmp_path):
    """ocr_enabled=False means skipped='disabled', regardless of has_images."""
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fail(*a, **k):
        raise AssertionError("should not run")

    _, _, ocr_obj, _ = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=False, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        has_images=False, ocr_all=False,
        _qr_fn=fail, _ocr_fn=fail,
    )
    assert ocr_obj["skipped"] == "disabled"


def test_blank_takes_priority_over_no_images(tmp_path):
    """is_blank=True means skipped='blank_page', even if has_images=True."""
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fail(*a, **k):
        raise AssertionError("should not run")

    _, _, ocr_obj, _ = _process_page_scanners(
        tmp_path, rec, is_blank=True,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        has_images=True, ocr_all=False,
        _qr_fn=fail, _ocr_fn=fail,
    )
    assert ocr_obj["skipped"] == "blank_page"


def test_default_has_images_true_is_backward_compatible(tmp_path):
    """Existing callers without has_images get the old 'run OCR' behavior."""
    from clippyshot.ocr import OCRResult
    rec = {"index": 1, "file": "page-001.png"}
    (tmp_path / "page-001.png").write_bytes(b"fake")

    def fake_ocr(path, **kwargs):
        return OCRResult(text="back-compat", char_count=11, duration_ms=5)

    _, _, ocr_obj, _ = _process_page_scanners(
        tmp_path, rec, is_blank=False,
        qr_enabled=False, qr_formats="qr_code", qr_timeout_s=10,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=6, ocr_time_left=lambda: 60.0,
        # has_images defaults to True, ocr_all defaults to False
        _qr_fn=lambda *a, **k: [], _ocr_fn=fake_ocr,
    )
    assert ocr_obj["text"] == "back-compat"
    assert "skipped" not in ocr_obj
