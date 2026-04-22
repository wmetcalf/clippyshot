"""Unit tests for the per-page image selection helper used by QR/OCR."""
from __future__ import annotations

from pathlib import Path

from clippyshot.converter import select_scan_image


def test_prefers_focused_when_present(tmp_path):
    out = tmp_path
    (out / "page-001.png").write_bytes(b"o")
    (out / "page-001-trimmed.png").write_bytes(b"t")
    (out / "page-001-focused.png").write_bytes(b"f")
    rec = {
        "file": "page-001.png",
        "trimmed": {"file": "page-001-trimmed.png"},
        "focused": {"file": "page-001-focused.png"},
    }
    assert select_scan_image(out, rec) == out / "page-001-focused.png"


def test_prefers_trimmed_over_original(tmp_path):
    out = tmp_path
    (out / "page-001.png").write_bytes(b"o")
    (out / "page-001-trimmed.png").write_bytes(b"t")
    rec = {
        "file": "page-001.png",
        "trimmed": {"file": "page-001-trimmed.png"},
    }
    assert select_scan_image(out, rec) == out / "page-001-trimmed.png"


def test_falls_back_to_original(tmp_path):
    out = tmp_path
    (out / "page-001.png").write_bytes(b"o")
    rec = {"file": "page-001.png"}
    assert select_scan_image(out, rec) == out / "page-001.png"


def test_returns_none_when_original_missing(tmp_path):
    out = tmp_path
    rec = {"file": "page-001.png"}
    assert select_scan_image(out, rec) is None
