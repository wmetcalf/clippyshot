"""Dispatcher `_validate_metadata` accepts scanner fields and caps sizes."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from clippyshot.dispatcher import Dispatcher


def _make_dispatcher() -> Dispatcher:
    return Dispatcher(job_store=MagicMock(), image_name="clippyshot:test")


def _valid_meta(pages=None):
    pages = pages or [{
        "index": 1, "file": "page-001.png",
        "qr": [], "ocr": {"text": "hi", "char_count": 2, "duration_ms": 1},
    }]
    return {
        "render": {
            "page_count_total": len(pages),
            "page_count_rendered": len(pages),
            "scanners": {
                "qr": {"enabled": True, "formats": "qr_code"},
                "ocr": {"enabled": True, "lang": "eng", "psm": 6},
            },
        },
        "pages": pages,
    }


def test_valid_metadata_passes(tmp_path):
    d = _make_dispatcher()
    assert d._validate_metadata(_valid_meta(), tmp_path) is True


def test_missing_qr_key_rejected(tmp_path):
    d = _make_dispatcher()
    meta = _valid_meta([{"index": 1, "file": "page-001.png",
                         "ocr": {"text": "", "char_count": 0, "duration_ms": 0}}])
    assert d._validate_metadata(meta, tmp_path) is False


def test_missing_ocr_key_rejected(tmp_path):
    d = _make_dispatcher()
    meta = _valid_meta([{"index": 1, "file": "page-001.png", "qr": []}])
    assert d._validate_metadata(meta, tmp_path) is False


def test_qr_list_too_long_rejected(tmp_path):
    d = _make_dispatcher()
    huge_qr = [{"format": "qr_code", "value": "x"} for _ in range(1001)]
    meta = _valid_meta([{
        "index": 1, "file": "page-001.png",
        "qr": huge_qr,
        "ocr": {"text": "", "char_count": 0, "duration_ms": 0},
    }])
    assert d._validate_metadata(meta, tmp_path) is False


def test_qr_string_field_too_large_is_truncated_not_rejected(tmp_path):
    d = _make_dispatcher()
    oversize = "x" * (64 * 1024 + 1)
    meta = _valid_meta([{
        "index": 1, "file": "page-001.png",
        "qr": [{"format": "qr_code", "value": oversize}],
        "ocr": {"text": "", "char_count": 0, "duration_ms": 0},
    }])
    assert d._validate_metadata(meta, tmp_path) is True
    assert len(meta["pages"][0]["qr"][0]["value"]) == 64 * 1024
    codes = [w.get("code") for w in meta.get("warnings", [])]
    assert "qr_field_truncated" in codes


def test_ocr_text_over_1mb_is_truncated_not_rejected(tmp_path):
    d = _make_dispatcher()
    big_text = "a" * (2 * 1024 * 1024)
    meta = _valid_meta([{
        "index": 1, "file": "page-001.png",
        "qr": [],
        "ocr": {"text": big_text, "char_count": len(big_text), "duration_ms": 0},
    }])
    assert d._validate_metadata(meta, tmp_path) is True
    assert len(meta["pages"][0]["ocr"]["text"]) == 1024 * 1024


def test_missing_render_scanners_rejected(tmp_path):
    d = _make_dispatcher()
    meta = _valid_meta()
    del meta["render"]["scanners"]
    assert d._validate_metadata(meta, tmp_path) is False
