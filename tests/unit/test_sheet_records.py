"""Sheet type/state from a binary workbook's own records (xls BoundSheet8 / xlsb BrtBundleSh).

LibreOffice's xlsx re-export (the two-pass path every xls/xlsb takes) writes an XLM macro
sheet as a worksheet and veryHidden as hidden; these tests pin that the inventory now comes
from the original's records when they align, and falls back otherwise.
"""
from __future__ import annotations

import shutil
import struct
import zipfile
from pathlib import Path

import olefile
import pytest

from clippyshot.libreoffice import sheet_records as sr
from clippyshot.libreoffice.runner import LibreOfficeRunner
from clippyshot.limits import Limits
from clippyshot.types import SandboxResult

FIXTURE_XLS = Path(__file__).resolve().parents[1] / "fixtures" / "safe" / "fixture.xls"


# ─── BIFF8 builders ──────────────────────────────────────────────────────────

def _rec(rtype: int, body: bytes) -> bytes:
    return struct.pack("<HH", rtype, len(body)) + body


def _bof(vers: int = 0x0600, dt: int = 0x0005) -> bytes:
    return _rec(0x0809, struct.pack("<HHHHII", vers, dt, 0, 0, 0, 0))


def _boundsheet(name: str, hs: int, dt: int, *, wide: bool = False, reserved: int = 0) -> bytes:
    enc = name.encode("utf-16-le" if wide else "latin-1")
    return _rec(0x0085, struct.pack("<IBBBB", 0, (reserved << 2) | hs, dt, len(name),
                                    1 if wide else 0) + enc)


def _globals(*records: bytes) -> bytes:
    return _bof() + _rec(0x0042, b"\xe4\x04") + b"".join(records) + _rec(0x000A, b"")


def test_biff8_boundsheets_carry_state_and_type():
    data = _globals(
        _boundsheet("Sheet1", 0, 0),
        _boundsheet("Macro1", 2, 1),             # very hidden XLM macro sheet
        _boundsheet("Chart1", 1, 2),
        _boundsheet("Module1", 0, 6),
        _boundsheet("Ст", 1, 0, wide=True),
        _boundsheet("Res", 0, 0, reserved=0x3F),  # reserved bits are ignored
    )
    assert sr.parse_biff8_globals(data) == [
        {"name": "Sheet1", "state": "visible", "type": "worksheet"},
        {"name": "Macro1", "state": "veryHidden", "type": "macro"},
        {"name": "Chart1", "state": "hidden", "type": "chartsheet"},
        {"name": "Module1", "state": "visible", "type": "vba_module"},
        {"name": "Ст", "state": "hidden", "type": "worksheet"},
        {"name": "Res", "state": "visible", "type": "worksheet"},
    ]


def test_biff8_stops_at_the_globals_eof():
    # A sheet substream after the globals EOF is never read as sheet metadata.
    data = _globals(_boundsheet("A", 0, 0)) + _bof(dt=0x0010) + _boundsheet("Z", 2, 1)
    assert [s["name"] for s in sr.parse_biff8_globals(data)] == ["A"]


@pytest.mark.parametrize("data", [
    _bof(vers=0x0500) + _boundsheet("A", 0, 0) + _rec(0x000A, b""),   # BIFF5
    _bof(dt=0x0010) + _boundsheet("A", 0, 0) + _rec(0x000A, b""),     # not globals
    _bof() + _rec(0x002F, b"\x01\x00" + b"\0" * 52) + _boundsheet("A", 0, 0)
    + _rec(0x000A, b""),                                               # encrypted
    _bof() + _boundsheet("A", 0, 0),                                   # no EOF
    b"",
])
def test_biff8_declines_what_it_cannot_read(data):
    assert sr.parse_biff8_globals(data) is None


@pytest.mark.parametrize("bad", [
    _globals(_boundsheet("A", 3, 0)),                       # undefined hsState
    _globals(_boundsheet("A", 0, 7)),                       # undefined dt
    _globals(_rec(0x0085, b"\0" * 7)),                       # short record
    _globals(_rec(0x0085, struct.pack("<IBBBB", 0, 0, 0, 40, 0) + b"short")),
    _bof() + struct.pack("<HH", 0x0085, 200) + b"\0" * 10,  # truncated record
])
def test_biff8_malformed_records_are_refused(bad):
    with pytest.raises(sr._Bad):
        sr.parse_biff8_globals(bad)


def test_biff8_sheet_count_is_bounded(monkeypatch):
    monkeypatch.setattr(sr, "MAX_RECORD_SHEETS", 3)
    data = _globals(*[_boundsheet(f"S{i}", 0, 0) for i in range(4)])
    assert sr.parse_biff8_globals(data) is None


def _patched_fixture(tmp_path: Path, hs: int, dt: int) -> Path:
    """The repo's real fixture.xls with its one BoundSheet8's hsState/dt rewritten."""
    out = tmp_path / "book.xls"
    shutil.copy(FIXTURE_XLS, out)
    with olefile.OleFileIO(str(out), write_mode=True) as ole:
        data = bytearray(ole.openstream("Workbook").read())
        pos = 0
        while True:
            rtype, rlen = struct.unpack_from("<HH", data, pos)
            if rtype == 0x0085:
                data[pos + 8] = hs
                data[pos + 9] = dt
                break
            pos += 4 + rlen
        ole.write_stream("Workbook", bytes(data))
    return out


def test_real_fixture_xls_reads_its_one_sheet():
    assert sr.read_binary_sheet_records(FIXTURE_XLS) == (
        sr.XLS_SOURCE, [{"name": "fixture", "state": "visible", "type": "worksheet"}])


def test_crafted_xls_reports_a_very_hidden_macro_sheet(tmp_path):
    p = _patched_fixture(tmp_path, hs=2, dt=1)
    assert sr.read_binary_sheet_records(p) == (
        sr.XLS_SOURCE, [{"name": "fixture", "state": "veryHidden", "type": "macro"}])


def test_oversized_workbook_stream_is_declined(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "MAX_BIFF_STREAM_BYTES", 100)
    assert sr.read_binary_sheet_records(FIXTURE_XLS) is None


def test_non_workbooks_are_declined(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n")
    assert sr.read_binary_sheet_records(p) is None
    ods = tmp_path / "x.ods"
    with zipfile.ZipFile(ods, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
    assert sr.read_binary_sheet_records(ods) is None


# ─── xlsb builders ───────────────────────────────────────────────────────────

def _varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def _brt(rtype: int, body: bytes = b"") -> bytes:
    return _varint(rtype) + _varint(len(body)) + body


def _wstr(s: str | None) -> bytes:
    if s is None:
        return struct.pack("<I", 0xFFFFFFFF)
    return struct.pack("<I", len(s)) + s.encode("utf-16-le")


def _bundle_sh(name: str, hs: int, rid: str | None, tab: int = 1) -> bytes:
    return _brt(156, struct.pack("<II", hs, tab) + _wstr(rid) + _wstr(name))


_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_REL_MS = "http://schemas.microsoft.com/office/2006/relationships/"


_XLSB_CT = "application/vnd.ms-excel.sheet.binary.macroEnabled.main"
_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _package(z: zipfile.ZipFile, target: str = "xl/workbook.bin",
             ct: str = _XLSB_CT, rel_type: str = _REL + "officeDocument") -> None:
    z.writestr("_rels/.rels", f'<Relationships xmlns="{_PKG_RELS}"><Relationship Id="rId1" '
                              f'Type="{rel_type}" Target="{target}"/></Relationships>')
    z.writestr("[Content_Types].xml",
               '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               f'<Override PartName="/xl/workbook.bin" ContentType="{ct}"/></Types>')


def _xlsb(tmp_path: Path, records: bytes, rels: dict[str, str], name="book.xlsb",
          **package) -> Path:
    p = tmp_path / name
    rel_xml = "".join(f'<Relationship Id="{k}" Type="{v}" Target="x"/>' for k, v in rels.items())
    with zipfile.ZipFile(p, "w") as z:
        _package(z, **package)
        z.writestr("xl/workbook.bin", records)
        z.writestr("xl/_rels/workbook.bin.rels",
                   f'<Relationships xmlns="{_PKG_RELS}">{rel_xml}</Relationships>')
    return p


def _book(*sheets: bytes) -> bytes:
    # BrtBeginBook(131), BrtFileVersion(128, some payload), BrtBeginBundleShs(143) ...
    return (_brt(131, b"") + _brt(128, b"\x00" * 20) + _brt(143, b"") + b"".join(sheets)
            + _brt(144, b"") + _brt(132, b""))


def test_xlsb_bundle_sheets_with_types_from_rels(tmp_path):
    p = _xlsb(tmp_path, _book(_bundle_sh("Sheet1", 0, "rId1"),
                              _bundle_sh("Macro1", 2, "rId2"),
                              _bundle_sh("Chart1", 1, "rId3")),
              {"rId1": _REL + "worksheet", "rId2": _REL_MS + "xlMacrosheet",
               "rId3": _REL + "chartsheet"})
    assert sr.read_binary_sheet_records(p) == (sr.XLSB_SOURCE, [
        {"name": "Sheet1", "state": "visible", "type": "worksheet"},
        {"name": "Macro1", "state": "veryHidden", "type": "macro"},
        {"name": "Chart1", "state": "hidden", "type": "chartsheet"},
    ])


def test_xlsb_two_byte_record_types_are_decoded():
    # A 2-byte type (>127) before the sheets must not desync the walk.
    data = (_brt(131, b"") + _brt(2000, b"\x01" * 300) + _brt(143, b"")
            + _bundle_sh("S", 1, "rId1") + _brt(144, b""))
    assert sr.parse_xlsb_bundle_sheets(data) == [("S", "hidden", "rId1")]


@pytest.mark.parametrize("records,rels", [
    (_book(_bundle_sh("S", 0, "rId7")), {"rId1": _REL + "worksheet"}),   # unresolved rid
    (_book(_bundle_sh("S", 5, "rId1")), {"rId1": _REL + "worksheet"}),   # bad state
    (_book(), {"rId1": _REL + "worksheet"}),                             # no sheets
    (_brt(156, b"\0" * 4), {"rId1": _REL + "worksheet"}),                # short record
    (b"\x9c\x01\xff\xff\xff\xff\x7f", {"rId1": _REL + "worksheet"}),     # overlong size
])
def test_xlsb_declines_what_it_cannot_read(tmp_path, records, rels):
    assert sr.read_binary_sheet_records(_xlsb(tmp_path, records, rels)) is None


def test_xlsb_without_rels_is_declined(tmp_path):
    p = tmp_path / "b.xlsb"
    with zipfile.ZipFile(p, "w") as z:
        _package(z)
        z.writestr("xl/workbook.bin", _book(_bundle_sh("S", 0, "rId1")))
    assert sr.read_binary_sheet_records(p) is None


def test_xlsb_duplicate_relationship_id_is_declined(tmp_path):
    p = tmp_path / "b.xlsb"
    with zipfile.ZipFile(p, "w") as z:
        _package(z)
        z.writestr("xl/workbook.bin", _book(_bundle_sh("S", 0, "rId1")))
        z.writestr("xl/_rels/workbook.bin.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   f'relationships"><Relationship Id="rId1" Type="{_REL}worksheet" Target="a"/>'
                   f'<Relationship Id="rId1" Type="{_REL_MS}xlMacrosheet" Target="b"/>'
                   '</Relationships>')
    assert sr.read_binary_sheet_records(p) is None


# ─── overlay onto the re-export inventory ────────────────────────────────────

def _reexport(*names: str) -> list[dict]:
    return [{"name": n, "state": "hidden" if n.startswith("H") else "visible",
             "type": "worksheet", "active": i == 0} for i, n in enumerate(names)]


def test_overlay_takes_type_and_state_from_the_records(tmp_path):
    p = _patched_fixture(tmp_path, hs=2, dt=1)
    sheets, source = sr.overlay_binary_records(p, _reexport("fixture"))
    assert source == sr.XLS_SOURCE
    assert sheets == [{"name": "fixture", "state": "veryHidden", "type": "macro",
                       "active": True}]


def test_overlay_keeps_vba_module_sheets_libreoffice_hides(tmp_path, monkeypatch):
    # LibreOffice creates a (hidden) tab for a VBA module BoundSheet8, so its re-export
    # lists it: module sheets stay in the alignment.
    recs = [{"name": "A", "state": "visible", "type": "worksheet"},
            {"name": "Module1", "state": "visible", "type": "vba_module"},
            {"name": "M", "state": "veryHidden", "type": "macro"}]
    monkeypatch.setattr(sr, "read_binary_sheet_records", lambda p: (sr.XLS_SOURCE, recs))
    sheets, _ = sr.overlay_binary_records(tmp_path / "x", _reexport("A", "Module1", "M"))
    assert [(s["name"], s["state"], s["type"]) for s in sheets] == [
        ("A", "visible", "worksheet"), ("Module1", "visible", "vba_module"),
        ("M", "veryHidden", "macro")]
    assert sr.overlay_binary_records(tmp_path / "x", _reexport("A", "M")) is None


@pytest.mark.parametrize("names", [("A",), ("A", "N"), ("M", "A"), ("A", "M", "X")])
def test_overlay_refuses_lists_that_do_not_align(tmp_path, monkeypatch, names):
    recs = [{"name": "A", "state": "visible", "type": "worksheet"},
            {"name": "M", "state": "veryHidden", "type": "macro"}]
    monkeypatch.setattr(sr, "read_binary_sheet_records", lambda p: (sr.XLS_SOURCE, recs))
    assert sr.overlay_binary_records(tmp_path / "x", _reexport(*names)) is None


# ─── runner wiring: the two-pass capture uses the original's records ─────────

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class _TwoPassSandbox:
    """--convert-to xlsx writes a re-export whose sheet is a plain hidden worksheet (what
    LO does to a veryHidden XLM macro sheet); --convert-to pdf writes a PDF."""

    name = "fake-two-pass"

    def __init__(self, sheet_name: str) -> None:
        self.sheet_name = sheet_name

    def run(self, request):
        conv = request.argv[request.argv.index("--convert-to") + 1]
        stem = Path(request.argv[-1]).stem
        if conv == "xlsx":
            out = next(m.host_path for m in request.rw_mounts
                       if m.sandbox_path == Path("/sandbox/xlsx_conv"))
            with zipfile.ZipFile(out / f"{stem}.xlsx", "w") as z:
                z.writestr("xl/workbook.xml",
                           f'<workbook xmlns="{_MAIN}"><sheets><sheet name="{self.sheet_name}"'
                           ' state="hidden" sheetId="1"/></sheets></workbook>')
        else:
            out = next(m.host_path for m in request.rw_mounts
                       if m.sandbox_path == Path("/sandbox/out"))
            (out / f"{stem}.pdf").write_bytes(b"%PDF-1.4\nfake\n%%EOF\n")
        return SandboxResult(exit_code=0, stdout=b"", stderr=b"", duration_ms=5, killed=False)

    def smoketest(self):
        return SandboxResult(0, b"", b"", 1, False)


def test_two_pass_inventory_comes_from_the_xls_records(tmp_path):
    src = _patched_fixture(tmp_path, hs=2, dt=1)
    runner = LibreOfficeRunner(sandbox=_TwoPassSandbox("fixture"), uno_server=None)
    runner.convert_to_pdf(src, tmp_path / "out", Limits(), "xls")
    assert runner.last_run_sheets_from_reexport is True
    assert runner.last_run_sheets_source == sr.XLS_SOURCE
    assert [(s["name"], s["state"], s["type"]) for s in runner.last_run_sheets] == [
        ("fixture", "veryHidden", "macro")]


def test_two_pass_keeps_the_reexport_when_the_records_do_not_align(tmp_path):
    src = _patched_fixture(tmp_path, hs=2, dt=1)
    runner = LibreOfficeRunner(sandbox=_TwoPassSandbox("Renamed"), uno_server=None)
    runner.convert_to_pdf(src, tmp_path / "out", Limits(), "xls")
    assert runner.last_run_sheets_source == "libreoffice_xlsx_reexport"
    assert [(s["name"], s["state"], s["type"]) for s in runner.last_run_sheets] == [
        ("Renamed", "hidden", "worksheet")]


def test_source_resets_between_jobs(tmp_path):
    src = _patched_fixture(tmp_path, hs=2, dt=1)
    runner = LibreOfficeRunner(sandbox=_TwoPassSandbox("fixture"), uno_server=None)
    runner.convert_to_pdf(src, tmp_path / "a", Limits(), "xls")
    doc = tmp_path / "w.docx"
    doc.write_bytes(b"x")
    runner._sandbox = _TwoPassSandbox("fixture")
    runner.convert_to_pdf(doc, tmp_path / "b", Limits(), "docx")
    assert runner.last_run_sheets_source == "input_workbook"
    assert runner.last_run_sheets == []


@pytest.mark.parametrize("attr,expected", [
    (sr.XLS_SOURCE, sr.XLS_SOURCE),
    (sr.XLSB_SOURCE, sr.XLSB_SOURCE),
    ("libreoffice_xlsx_reexport", "libreoffice_xlsx_reexport"),
    ("<attacker>", "input_workbook"),   # only known values are emitted
])
def test_converter_reports_the_runner_inventory_source(attr, expected):
    from clippyshot.converter import _inventory_source

    class R:
        last_run_sheets_source = attr
        last_run_sheets_from_reexport = False

    assert _inventory_source(R()) == expected


@pytest.mark.parametrize("package", [
    {"target": "xl/decoy.bin"},                        # LO imports another part (codex on #68)
    {"rel_type": "urn:decoy/officeDocument"},           # LO's detector wants the exact URI
    {"ct": "application/vnd.ms-excel.sheet.macroEnabled.main+xml"},  # not the binary importer
])
def test_xlsb_records_only_from_the_part_libreoffice_imports(tmp_path, package):
    p = _xlsb(tmp_path, _book(_bundle_sh("S", 2, "rId1")),
              {"rId1": _REL_MS + "xlMacrosheet"}, **package)
    assert sr.read_binary_sheet_records(p) is None


def test_xlsb_absolute_package_target_is_accepted(tmp_path):
    p = _xlsb(tmp_path, _book(_bundle_sh("S", 2, "rId1")),
              {"rId1": _REL_MS + "xlMacrosheet"}, target="/xl/workbook.bin")
    assert sr.read_binary_sheet_records(p) == (
        sr.XLSB_SOURCE, [{"name": "S", "state": "veryHidden", "type": "macro"}])


# ─── records only from what LibreOffice actually imports ────────────────────

def test_xls_with_an_embedded_or_appended_zip_is_still_read_as_xls(tmp_path):
    """Excel 2007+ .xls carry zips (THEME / MSODRAWING) in the Workbook stream, which
    zipfile.is_zipfile() accepts; an attacker can also append a decoy xlsb package."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        _package(z)
        z.writestr("xl/workbook.bin", _book(_bundle_sh("fixture", 0, "rId1")))
        z.writestr("xl/_rels/workbook.bin.rels",
                   f'<Relationships xmlns="{_PKG_RELS}"><Relationship Id="rId1" '
                   f'Type="{_REL}worksheet" Target="x"/></Relationships>')
    src = _patched_fixture(tmp_path, hs=2, dt=1)
    poly = tmp_path / "poly.xls"
    poly.write_bytes(src.read_bytes() + buf.getvalue())
    assert zipfile.is_zipfile(poly)
    assert sr.read_binary_sheet_records(poly) == (
        sr.XLS_SOURCE, [{"name": "fixture", "state": "veryHidden", "type": "macro"}])


def test_xlsb_decoy_sheet_block_outside_the_book_is_declined():
    real = (_brt(131) + _brt(143) + _bundle_sh("Alpha", 0, "rId1")
            + _bundle_sh("Beta", 2, "rId2") + _brt(144) + _brt(132))
    decoy = _brt(143) + _bundle_sh("Alpha", 0, "rId1") + _bundle_sh("Beta", 0, "rId1") + _brt(144)
    assert sr.parse_xlsb_bundle_sheets(real) == [("Alpha", "visible", "rId1"),
                                                 ("Beta", "veryHidden", "rId2")]
    assert sr.parse_xlsb_bundle_sheets(decoy + real) is None
    second = real[:-len(_brt(132))] + decoy + _brt(132)
    assert sr.parse_xlsb_bundle_sheets(second) is None
    stray = real[:-len(_brt(132))] + _bundle_sh("Gamma", 0, "rId3") + _brt(132)
    assert sr.parse_xlsb_bundle_sheets(stray) is None


def test_xls_with_both_book_and_workbook_streams_is_declined(tmp_path):
    p = tmp_path / "two.xls"
    shutil.copy(FIXTURE_XLS, p)
    with olefile.OleFileIO(str(p)) as ole:
        names = ["/".join(e) for e in ole.listdir()]
    assert "Book" not in names
    # Rename a same-length stream entry to "Book" in the directory (both are 4 chars:
    # "\x01Ole" -> "Book" keeps the CFB valid).
    raw = bytearray(p.read_bytes())
    old = "\x01Ole".encode("utf-16-le")
    i = raw.find(old)
    assert i > 0
    raw[i:i + len(old)] = "Book".encode("utf-16-le")
    p.write_bytes(bytes(raw))
    with olefile.OleFileIO(str(p)) as ole:
        assert ole.exists("Book") and ole.exists("Workbook")
    assert sr.read_binary_sheet_records(p) is None


def test_biff8_bof_longer_than_libreoffice_accepts_is_declined():
    data = (_rec(0x0809, struct.pack("<HHHHII", 0x0600, 0x0005, 0, 0, 0, 0) + b"\0" * 4)
            + _boundsheet("A", 0, 0) + _rec(0x000A, b""))
    assert sr.parse_biff8_globals(data) is None


def test_xlsb_null_relationship_id_is_a_vba_module_sheet(tmp_path):
    p = _xlsb(tmp_path, _book(_bundle_sh("M", 2, "rId1"), _bundle_sh("Module1", 2, None)),
              {"rId1": _REL_MS + "xlMacrosheet"})
    assert sr.read_binary_sheet_records(p) == (sr.XLSB_SOURCE, [
        {"name": "M", "state": "veryHidden", "type": "macro"},
        {"name": "Module1", "state": "veryHidden", "type": "vba_module"}])


def test_xlsb_truncated_inside_the_sheet_block_is_declined():
    data = _brt(131) + _brt(143) + _bundle_sh("A", 0, "rId1")  # no BrtEndBundleShs
    assert sr.parse_xlsb_bundle_sheets(data) is None
