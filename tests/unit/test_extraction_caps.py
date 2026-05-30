"""Decompression-bomb + XXE guards for the in-process extractors.

These run before/around the sandbox with full service privileges, so an
unbounded ``zf.read`` or an entity-expansion bomb here is a real DoS / XXE
even though the eventual soffice render is sandboxed.
"""
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from clippyshot.libreoffice import altchunk, sheet_prep
from clippyshot.libreoffice._safexml import safe_fromstring
from clippyshot.libreoffice._safezip import (
    ExtractionBudget,
    ExtractionLimitExceeded,
    bounded_read,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------
# bounded_read / ExtractionBudget
# --------------------------------------------------------------------------

def test_bounded_read_rejects_oversized_entry():
    raw = _zip_bytes({"big.bin": b"A" * 5000})
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        # Under the cap: fine.
        assert bounded_read(zf, "big.bin", max_bytes=5000) == b"A" * 5000
        # Over the cap: refused before materializing the whole entry.
        with pytest.raises(ExtractionLimitExceeded):
            bounded_read(zf, "big.bin", max_bytes=100)


def test_bounded_read_streaming_guard_on_compressible_bomb():
    # 4 MiB of zeros compresses to a few KB (a real bomb shape). The cap must
    # fire on the decompressed stream, not the compressed footprint.
    raw = _zip_bytes({"bomb.bin": b"\x00" * (4 * 1024 * 1024)})
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert zf.getinfo("bomb.bin").compress_size < 64 * 1024
        with pytest.raises(ExtractionLimitExceeded):
            bounded_read(zf, "bomb.bin", max_bytes=256 * 1024)


def test_budget_caps_cumulative_bytes():
    raw = _zip_bytes({f"e{i}.bin": b"A" * 1000 for i in range(5)})
    budget = ExtractionBudget(max_total=1500, max_entries=100)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        budget.read(zf, "e0.bin")  # 1000 ok
        with pytest.raises(ExtractionLimitExceeded):
            budget.read(zf, "e1.bin")  # would push past 1500 cumulative


def test_budget_caps_entry_count():
    raw = _zip_bytes({f"e{i}.bin": b"x" for i in range(5)})
    budget = ExtractionBudget(max_total=10**9, max_entries=2)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        budget.read(zf, "e0.bin")
        budget.read(zf, "e1.bin")
        with pytest.raises(ExtractionLimitExceeded):
            budget.read(zf, "e2.bin")


# --------------------------------------------------------------------------
# safe_fromstring (XXE / billion-laughs)
# --------------------------------------------------------------------------

_BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;">
]>
<root>&lol2;</root>"""

_XXE = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<foo>&xxe;</foo>"""


@pytest.mark.parametrize("payload", [_BILLION_LAUGHS, _XXE])
def test_safe_fromstring_rejects_dtd(payload):
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload)


def test_safe_fromstring_parses_clean_xml():
    el = safe_fromstring(b"<a><b>hi</b></a>")
    assert el.find("b").text == "hi"


# --------------------------------------------------------------------------
# Extractor wiring: a low-capped budget must trip on a bomb entry.
# --------------------------------------------------------------------------

def _low_budget():
    return ExtractionBudget(max_total=4096, max_entries=50)


def test_patch_ooxml_for_print_bounded(monkeypatch, tmp_path: Path):
    # A worksheet entry larger than the (patched-low) cap must raise, so the
    # runner routes to the sandboxed two-pass path instead of OOMing.
    raw = _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "xl/worksheets/sheet1.xml": b"<worksheet>" + b"A" * 20000 + b"</worksheet>",
        }
    )
    p = tmp_path / "big.xlsx"
    p.write_bytes(raw)
    monkeypatch.setattr(sheet_prep, "ExtractionBudget", _low_budget)
    with pytest.raises(ExtractionLimitExceeded):
        sheet_prep.patch_ooxml_for_print(p)


def test_inspect_altchunks_skips_bomb_part(monkeypatch, tmp_path: Path):
    # altChunk part bigger than the cap is skipped (not returned), and the
    # call still succeeds rather than blowing up the worker.
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/word/afchunk.mht" ContentType="message/rfc822"/>'
        b"</Types>"
    )
    document_xml = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        b' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        b'<w:body><w:altChunk r:id="rId1"/></w:body></w:document>'
    )
    rels_xml = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1"'
        b' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"'
        b' Target="afchunk.mht"/></Relationships>'
    )
    raw = _zip_bytes(
        {
            "[Content_Types].xml": content_types,
            "word/document.xml": document_xml,
            "word/_rels/document.xml.rels": rels_xml,
            "word/afchunk.mht": b"M" * 20000,  # bomb part > low cap
        }
    )
    p = tmp_path / "bomb.docx"
    p.write_bytes(raw)
    monkeypatch.setattr(altchunk, "ExtractionBudget", _low_budget)
    # Must not raise; the oversized part is simply not returned.
    assert altchunk.inspect_altchunks(p) == []


def test_inspect_altchunks_rejects_dtd_document(tmp_path: Path):
    raw = _zip_bytes(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": _XXE,
            "word/_rels/document.xml.rels": b"<Relationships/>",
        }
    )
    p = tmp_path / "xxe.docx"
    p.write_bytes(raw)
    # safe_fromstring raises ParseError on the DOCTYPE → graceful empty result.
    assert altchunk.inspect_altchunks(p) == []
