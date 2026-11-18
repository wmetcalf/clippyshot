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


# --------------------------------------------------------------------------
# safe_fromstring must reject DTDs regardless of the document encoding. The old
# guard was a byte regex for ASCII ``<!DOCTYPE`` and expat happily decodes a
# UTF-16/UTF-32 document whose DTD bytes are interleaved with NULs.
# --------------------------------------------------------------------------

def _encode(xml_text: str, codec: str, *, bom: bool) -> bytes:
    boms = {
        "utf-16-le": b"\xff\xfe",
        "utf-16-be": b"\xfe\xff",
        "utf-32-le": b"\xff\xfe\x00\x00",
        "utf-32-be": b"\x00\x00\xfe\xff",
        "utf-8": b"\xef\xbb\xbf",
    }
    return (boms[codec] if bom else b"") + xml_text.encode(codec)


_ENCODED_DTD_PAYLOADS = [
    ('<?xml version="1.0" encoding="UTF-16"?>' + _BILLION_LAUGHS.decode().split("?>", 1)[1]),
    ('<?xml version="1.0" encoding="UTF-16"?>' + _XXE.decode().split("?>", 1)[1]),
]


@pytest.mark.parametrize("text", _ENCODED_DTD_PAYLOADS)
@pytest.mark.parametrize(
    "codec,bom",
    [
        ("utf-16-le", True),
        ("utf-16-be", True),
        # No BOM: expat autodetects UTF-16 from the '<?' byte pattern.
        ("utf-16-le", False),
        ("utf-16-be", False),
    ],
)
def test_safe_fromstring_rejects_dtd_in_utf16(text, codec, bom):
    payload = _encode(text, codec, bom=bom)
    assert b"<!DOCTYPE" not in payload  # the old byte pre-scan cannot see it
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload)


@pytest.mark.parametrize("codec", ["utf-32-le", "utf-32-be"])
def test_safe_fromstring_rejects_utf32_dtd(codec):
    text = _ENCODED_DTD_PAYLOADS[1].replace("UTF-16", "UTF-32")
    with pytest.raises(ET.ParseError):
        safe_fromstring(_encode(text, codec, bom=True))


def test_safe_fromstring_rejects_dtd_via_declared_single_byte_encoding():
    # A declared non-UTF-8 encoding must not hide the DTD from the guard either.
    payload = '<?xml version="1.0" encoding="ISO-8859-1"?><!DOCTYPE a [<!ENTITY e "x">]><a>&e;</a>'
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload.encode("latin-1"))


def test_safe_fromstring_rejects_unparsed_and_parameter_entities():
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY % p "x">'
        b'<!NOTATION n SYSTEM "x"><!ENTITY u SYSTEM "x" NDATA n>]><a/>'
    )
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload.decode().encode("utf-16"))


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-8"])
def test_safe_fromstring_parses_clean_utf16_xml(codec):
    text = '<?xml version="1.0" encoding="UTF-16"?><a><b>hé</b></a>'
    if codec == "utf-8":
        text = text.replace("UTF-16", "UTF-8")
    el = safe_fromstring(_encode(text, codec, bom=True))
    assert el.find("b").text == "hé"


def test_safe_fromstring_rejects_utf16_entity_bomb():
    # A UTF-16 workbook.xml carrying an internal entity bomb must be refused
    # before any expansion, like any other DTD.
    bomb = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE w [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheets><sheet name="&b;" sheetId="1"/></sheets></workbook>'
    ).encode("utf-16")
    with pytest.raises(ET.ParseError):
        safe_fromstring(bomb)


@pytest.mark.parametrize("codec,declared", [
    ("utf-16-le", "utf_16_le"),  # expat refuses a DECLARED multi-byte codec it can't map
    ("utf-7", "utf-7"),
])
def test_safe_fromstring_raises_only_parse_error_on_unsupported_encoding(codec, declared):
    # Callers catch ET.ParseError; expat's "multi-byte encodings are not supported"
    # surfaced as ValueError and escaped them (altchunk inspection silently aborted).
    payload = f'<?xml version="1.0" encoding="{declared}"?><a/>'.encode(codec)
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload)


@pytest.mark.parametrize("enc", ["Shift_JIS", "bogus", "rot13"])
def test_safe_fromstring_unknown_or_multibyte_declared_encoding_is_parse_error(enc):
    # ValueError (multi-byte codec) / LookupError (unknown or non-text codec) used to
    # escape callers that only catch ParseError.
    with pytest.raises(ET.ParseError):
        safe_fromstring(f'<?xml version="1.0" encoding="{enc}"?><a/>'.encode())


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_safe_fromstring_rejects_declaration_free_wide_doctype(codec):
    # No <!ENTITY at all: only the DOCTYPE handler can refuse this (an external
    # subset is a fetch target for a parser that resolves it).
    payload = '<!DOCTYPE a SYSTEM "http://127.0.0.1:1/x.dtd"><a/>'.encode(codec)
    with pytest.raises(ET.ParseError):
        safe_fromstring(payload)
