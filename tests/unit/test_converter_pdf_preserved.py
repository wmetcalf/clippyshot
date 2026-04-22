"""Verify the converter preserves the intermediate PDF as output_dir/document.pdf."""
from __future__ import annotations

from pathlib import Path

import pytest

from clippyshot.converter import _copy_pdf_to_output


def test_copy_pdf_to_output_creates_document_pdf(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.7\n...body...\n%%EOF")
    out = tmp_path / "out"
    out.mkdir()

    dest = _copy_pdf_to_output(src, out)
    assert dest == out / "document.pdf"
    assert dest.read_bytes() == src.read_bytes()


def test_copy_pdf_to_output_idempotent(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.7\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "document.pdf").write_bytes(b"stale")

    _copy_pdf_to_output(src, out)
    assert (out / "document.pdf").read_bytes() == b"%PDF-1.7\n"


def test_copy_pdf_to_output_missing_source_returns_none(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = _copy_pdf_to_output(tmp_path / "nope.pdf", out)
    assert result is None
    assert not (out / "document.pdf").exists()
