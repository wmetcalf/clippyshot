"""Unit tests for the extended ConvertOptions scanner fields."""
from __future__ import annotations

from clippyshot.converter import ConvertOptions
from clippyshot.limits import Limits


def test_defaults_qr_on_ocr_off():
    opts = ConvertOptions(limits=Limits())
    assert opts.qr_enabled is True
    assert opts.ocr_enabled is False
    assert opts.qr_formats == "qr_code,micro_qr_code,rmqr_code"
    assert opts.ocr_lang == "eng"
    assert opts.ocr_psm == 6
    assert opts.ocr_timeout_s == 60
    assert opts.qr_timeout_s == 10


def test_overrides():
    opts = ConvertOptions(
        limits=Limits(),
        qr_enabled=False,
        ocr_enabled=True,
        ocr_lang="eng+deu",
        ocr_psm=11,
        ocr_timeout_s=120,
    )
    assert opts.qr_enabled is False
    assert opts.ocr_enabled is True
    assert opts.ocr_lang == "eng+deu"
    assert opts.ocr_psm == 11
    assert opts.ocr_timeout_s == 120
