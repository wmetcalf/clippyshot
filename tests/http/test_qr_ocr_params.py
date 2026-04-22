"""HTTP tests for the new QR/OCR request parameters."""
from __future__ import annotations

import pytest

from clippyshot.api import _parse_bool, _build_convert_options


def test_parse_bool_true_values():
    for v in ("1", "true", "True", "yes", "on", "y"):
        assert _parse_bool(v) is True


def test_parse_bool_false_values():
    for v in ("0", "false", "False", "no", "off", "n", ""):
        assert _parse_bool(v) is False


def test_parse_bool_none_is_default():
    assert _parse_bool(None, default=True) is True
    assert _parse_bool(None, default=False) is False


def test_convert_options_from_query_params():
    """_build_convert_options parses all the new params into ConvertOptions."""
    opts = _build_convert_options(
        qr="0",
        qr_formats="qr_code,data_matrix",
        ocr="1",
        ocr_lang="eng+deu",
        ocr_psm="11",
        ocr_timeout_s="120",
    )
    assert opts.qr_enabled is False
    assert opts.qr_formats == "qr_code,data_matrix"
    assert opts.ocr_enabled is True
    assert opts.ocr_lang == "eng+deu"
    assert opts.ocr_psm == 11
    assert opts.ocr_timeout_s == 120


def test_convert_options_defaults_from_env(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_ENABLE_QR", "0")
    monkeypatch.setenv("CLIPPYSHOT_ENABLE_OCR", "1")
    monkeypatch.setenv("CLIPPYSHOT_OCR_LANG", "fra")
    opts = _build_convert_options()
    assert opts.qr_enabled is False
    assert opts.ocr_enabled is True
    assert opts.ocr_lang == "fra"


def test_ocr_psm_out_of_range_rejected():
    with pytest.raises(ValueError):
        _build_convert_options(ocr_psm="99")


def test_ocr_timeout_out_of_range_rejected():
    with pytest.raises(ValueError):
        _build_convert_options(ocr_timeout_s="9999")
    with pytest.raises(ValueError):
        _build_convert_options(ocr_timeout_s="0")
