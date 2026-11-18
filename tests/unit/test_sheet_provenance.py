import pytest
import json
import zipfile
from pathlib import Path

from PIL import Image

from clippyshot.converter import Converter, ConvertOptions, _page_sheet_names, _sheet_provenance
from clippyshot.libreoffice.sheet_prep import MAX_SHEETS, read_sheet_inventory, read_sheet_list
from clippyshot.limits import Limits
from clippyshot.types import DetectedType, RasterizedPage

SHEETS=[{'name':'First','state':'visible','type':'worksheet'},
        {'name':'Hidden','state':'hidden','type':'worksheet'},
        {'name':'Last','state':'visible','type':'worksheet'}]

def test_single_page_export_keeps_hidden_sheet_in_page_order():
    assert _page_sheet_names(SHEETS,3,total_pages=3,single_page_sheets=True)==['First','Hidden','Last']
    coverage=_sheet_provenance(SHEETS,3,True,[{'index':1},{'index':2},{'index':3}],[])
    assert coverage['page_mapping_verified'] and coverage['rendered']==3
    assert coverage['non_rendered']==[] and coverage['inventory'][1]['state']=='hidden'
    assert coverage['inventory'][1]['page_number']==2


def test_ordinary_export_or_page_count_mismatch_does_not_guess_sheet_names():
    assert _page_sheet_names(SHEETS,3,total_pages=3,single_page_sheets=False)==[None]*3
    assert _page_sheet_names(SHEETS,2,total_pages=2,single_page_sheets=True)==[None]*2
    c=_sheet_provenance(SHEETS,2,True,[{'index':1},{'index':2}],[])
    assert not c['page_mapping_verified'] and c['rendered'] is None
    assert all(s['capture_status']=='unverified' for s in c['inventory'])


def test_blank_page_and_page_limit_are_distinct_from_hidden_sheet():
    c=_sheet_provenance(SHEETS,3,True,[{'index':1}],[2])
    assert [s['capture_status'] for s in c['inventory']]==['captured','blank_skipped','not_captured']
    assert [s['name'] for s in c['non_rendered']]==['Last']


# ── review round 1: reader parity, bounds, paginated fallback, convert() wiring ──

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_T = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def _workbook(tmp_path, body: str, name: str = "book.xlsx", rels: bool = False) -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS_NS}">{body}</workbook>')
        if rels:
            z.writestr("[Content_Types].xml",
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                       'content-types"><Override PartName="/xl/workbook.xml" ContentType="'
                       'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.'
                       'main+xml"/></Types>')
            z.writestr("_rels/.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                       f'relationships"><Relationship Id="rId1" Type="{_REL_T}officeDocument" '
                       'Target="xl/workbook.xml"/></Relationships>')
            z.writestr("xl/_rels/workbook.xml.rels",
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                       f'relationships"><Relationship Id="rId1" Type="{_REL_T}worksheet" '
                       f'Target="worksheets/sheet1.xml"/><Relationship Id="rId2" '
                       'Type="http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"'
                       ' Target="macrosheets/sheet1.xml"/></Relationships>')
    return p


def test_reader_flags_foreign_and_repeated_sheet_blocks(tmp_path):
    """A foreign-namespace <x:sheet> plus a second <sheets> block can give a same-length but
    different list from LO's: the reader reports every candidate and flags the workbook, so
    a count match is never taken as a positional mapping."""
    p = _workbook(tmp_path,
                  '<sheets><sheet name="A"/><x:sheet xmlns:x="urn:not-spreadsheetml" name="Decoy"/>'
                  '<sheet name="B"/></sheets><sheets><sheet name="PAYLOAD" state="hidden"/></sheets>')
    sheets, anomalies = read_sheet_inventory(p)
    assert [s["name"] for s in sheets] == ["A", "Decoy", "B", "PAYLOAD"]
    assert {"noncanonical_sheet_element", "multiple_sheets_blocks"} <= set(anomalies)
    c = _sheet_provenance(sheets[:3], 3, True, [{"index": i} for i in (1, 2, 3)], [],
                          anomalies=anomalies)
    assert c["page_mapping_verified"] is False and c["reader_anomalies"] == anomalies
    assert _page_sheet_names(sheets[:3], 3, total_pages=3, single_page_sheets=True,
                             anomalies=anomalies) == [None] * 3


@pytest.mark.parametrize("body,anomaly", [
    ('<sheets><sheet name="A" r:id="rId1"/><sheet r:id="rId2"/></sheets>', "unnamed_sheet"),
    ('<sheets><sheet name="Bad:Name" r:id="rId1"/></sheets>', "invalid_sheet_name"),
    ('<sheets><sheet name="A" r:id="rId1"/><sheet name="a" r:id="rId2"/></sheets>',
     "duplicate_sheet_name"),
    ('<sheets><sheet name="A" state="Hidden" r:id="rId1"/></sheets>', "nonstandard_sheet_state"),
    ('<sheets><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
     'markup-compatibility/2006"><mc:Fallback><sheet name="H" state="hidden" r:id="rId1"/>'
     '</mc:Fallback></mc:AlternateContent></sheets>', "markup_compatibility"),
    ('<sheets><sheet name="A" r:id="rId9"/></sheets>', "unresolved_sheet_relationship"),
])
def test_reader_flags_what_libreoffice_imports_differently(tmp_path, body, anomaly):
    p = _workbook(tmp_path, body, rels=True)
    assert anomaly in read_sheet_inventory(p)[1]


def test_excel_alternate_content_outside_the_sheet_list_is_not_an_anomaly(tmp_path):
    p = _workbook(tmp_path,
                  '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/'
                  'markup-compatibility/2006"><mc:Choice Requires="x15"><x15ac:absPath '
                  'xmlns:x15ac="http://schemas.microsoft.com/office/spreadsheetml/2010/11/ac" '
                  'url="C:\\x\\"/></mc:Choice></mc:AlternateContent>'
                  '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    assert read_sheet_inventory(p)[1] == []


def test_reader_flags_an_unqualified_root_libreoffice_does_not_import(tmp_path):
    p = tmp_path / "book.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<workbook><sheets><sheet name="Payload" state="hidden"/></sheets></workbook>')
    sheets, anomalies = read_sheet_inventory(p)
    assert [s["name"] for s in sheets] == ["Payload"] and "unsupported_root" in anomalies


def test_clean_workbook_has_no_anomalies_and_types_come_from_relationships(tmp_path):
    p = _workbook(tmp_path, '<sheets><sheet name="A" r:id="rId1"/>'
                            '<sheet name="M" state="hidden" r:id="rId2"/></sheets>', rels=True)
    sheets, anomalies = read_sheet_inventory(p)
    assert anomalies == []
    assert [(s["name"], s["type"]) for s in sheets] == [("A", "worksheet"), ("M", "macro")]


def test_reader_stops_one_past_the_sheet_cap(tmp_path):
    p = _workbook(tmp_path, "<sheets>" + "".join(
        f'<sheet name="s{i}"/>' for i in range(MAX_SHEETS + 50)) + "</sheets>")
    assert len(read_sheet_list(p)) == MAX_SHEETS + 1


def test_inventory_over_the_cap_is_truncated_and_unverified():
    sheets = [{"name": f"s{i}", "state": "visible", "type": "worksheet"}
              for i in range(MAX_SHEETS + 1)]
    c = _sheet_provenance(sheets, MAX_SHEETS + 1, True, [{"index": 1}], [])
    assert c["inventory_truncated"] is True and c["page_mapping_verified"] is False
    assert len(c["inventory"]) == MAX_SHEETS
    assert _page_sheet_names(sheets, 1, total_pages=MAX_SHEETS + 1,
                             single_page_sheets=True) == [None]


def test_paginated_calc_export_reports_hidden_sheets_not_captured():
    c = _sheet_provenance(SHEETS, 2, False, [{"index": 1}, {"index": 2}], [],
                          paginated_calc=True)
    assert [s["capture_status"] for s in c["inventory"]] == [
        "unverified", "not_captured", "unverified"]
    assert [s["name"] for s in c["non_rendered"]] == ["Hidden"]
    # Unknown export (no filter recorded): no claim either way.
    u = _sheet_provenance(SHEETS, 2, False, [{"index": 1}, {"index": 2}], [])
    assert u["non_rendered"] == []


class _SheetDetector:
    def detect(self, path, *, max_input_bytes=None):
        return DetectedType(label="xlsx", mime="application/vnd.ms-excel", extension_hint="xlsx",
                            confidence=0.99, source="magika", agreed_with_extension=True)


class _SheetRunner:
    def __init__(self, pages, sheets, single=True, export_filter="calc_pdf_Export",
                 anomalies=()):
        self.pages, self.sheets, self.single, self.filter = pages, sheets, single, export_filter
        self.anomalies = list(anomalies)
        self.last_run_sheet_anomalies: list = []
        self.last_run_sheets: list = []
        self.last_run_single_page_sheets = False
        self.last_run_export_filter = ""

    def convert_to_pdf(self, input_path, output_dir, limits, label="xlsx"):
        out = Path(output_dir) / "input.pdf"
        imgs = [Image.new("RGB", (612, 792), (255, 255, 255)) for _ in range(self.pages)]
        imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=72.0)
        self.last_run_sheets = list(self.sheets)
        self.last_run_sheet_anomalies = list(self.anomalies)
        self.last_run_single_page_sheets = self.single
        self.last_run_export_filter = self.filter
        return out


class _Raster:
    name = "stub"

    def __init__(self, blank=()):
        self.blank = set(blank)

    def rasterize(self, pdf_path, out_dir, dpi, max_pages, page_sizes_mm=None):
        res = []
        for i in range(1, min(3, max_pages) + 1):
            img = Image.new("RGB", (40, 40), (255, 255, 255))
            if i not in self.blank:
                img.putpixel((1, 1), (255, 0, 0))
                img.putpixel((20, 30), (0, 0, 255))
                for x in range(5, 35):
                    img.putpixel((x, 10), (0, 128, 0))
            img.save(Path(out_dir) / f"page-{i:03d}.png")
            res.append(RasterizedPage(index=i, path=f"page-{i:03d}.png", width_px=40,
                                      height_px=40, width_mm=10, height_mm=10))
        return res


def _convert(tmp_path, runner, limits, blank=()):
    src = tmp_path / "in.xlsx"
    src.write_bytes(b"x")
    out = tmp_path / "out"
    out.mkdir()
    Converter(detector=_SheetDetector(), runner=runner, rasterizer=_Raster(blank),
              sandbox_backend="bwrap", apparmor_profile="unconfined").convert(
        src, out, ConvertOptions(limits=limits))
    return json.loads((out / "metadata.json").read_text())


def test_convert_binds_names_on_a_capped_single_page_export(tmp_path):
    """Names bind against the FULL page count; the capped tail is not_captured."""
    meta = _convert(tmp_path, _SheetRunner(3, SHEETS), Limits(max_pages=2))
    assert [p.get("sheet_name") for p in meta["pages"]] == ["First", "Hidden"]
    sh = meta["sheets"]
    assert sh["page_mapping_verified"] is True and sh["exported"] == 3 and sh["rendered"] == 2
    assert [s["capture_status"] for s in sh["inventory"]] == ["captured", "captured", "not_captured"]


def test_convert_marks_a_skipped_blank_sheet(tmp_path):
    meta = _convert(tmp_path, _SheetRunner(3, SHEETS), Limits(skip_blanks=True), blank=(2,))
    assert [s["capture_status"] for s in meta["sheets"]["inventory"]] == [
        "captured", "blank_skipped", "captured"]


def test_convert_paginated_fallback_flags_the_hidden_sheet(tmp_path):
    meta = _convert(tmp_path, _SheetRunner(2, SHEETS, single=False), Limits())
    sh = meta["sheets"]
    assert sh["export_mode"] == "paginated_or_unknown" and sh["page_mapping_verified"] is False
    assert [s["name"] for s in sh["non_rendered"]] == ["Hidden"]
    assert all(p.get("sheet_name") is None for p in meta["pages"])


def test_convert_bounds_attacker_sheet_attributes(tmp_path):
    sheets = [{"name": "S", "state": "visible", "type": "t" * 5_000_000}]
    meta = _convert(tmp_path, _SheetRunner(3, sheets), Limits())
    assert len(meta["sheets"]["inventory"][0]["type"]) <= 32


def test_paginated_export_does_not_claim_the_active_sheet_was_dropped():
    """LO forces the active tab visible on import."""
    sheets = [{"name": "Active", "state": "hidden", "type": "worksheet", "active": True},
              {"name": "Shown", "state": "visible", "type": "worksheet", "active": False},
              {"name": "Gone", "state": "veryHidden", "type": "worksheet", "active": False}]
    c = _sheet_provenance(sheets, 2, False, [{"index": 1}, {"index": 2}], [],
                          paginated_calc=True)
    assert [s["capture_status"] for s in c["inventory"]] == [
        "unverified", "unverified", "not_captured"]
    # With reader anomalies (e.g. a non-spec state), nothing is claimed either way.
    a = _sheet_provenance(sheets, 2, False, [{"index": 1}, {"index": 2}], [],
                          paginated_calc=True, anomalies=["nonstandard_sheet_state"])
    assert all(s["capture_status"] == "unverified" for s in a["inventory"])


def test_reader_records_the_active_tab(tmp_path):
    p = _workbook(tmp_path, '<bookViews><workbookView activeTab="1"/></bookViews>'
                            '<sheets><sheet name="A"/><sheet name="B" state="hidden"/></sheets>')
    assert [(s["name"], s["active"]) for s in read_sheet_list(p)] == [("A", False), ("B", True)]


def test_sheet_names_are_bounded_in_escaped_json_bytes():
    from clippyshot.converter import _sheets_from_runner

    R = type("R", (), {"last_run_sheets": [
        {"name": "\U0001F600" * 300, "state": "\U0001F600" * 40, "type": "worksheet"}]})
    (s,) = _sheets_from_runner(R)
    assert len(json.dumps(s["name"])) <= 257 and len(json.dumps(s["state"])) <= 32


def test_active_tab_out_of_range_is_an_anomaly(tmp_path):
    p = _workbook(tmp_path, '<bookViews><workbookView activeTab="-1"/></bookViews>'
                            '<sheets><sheet name="H" state="hidden" r:id="rId1"/>'
                            '<sheet name="V" r:id="rId1"/></sheets>', rels=True)
    assert "bad_active_tab" in read_sheet_inventory(p)[1]


def test_foreign_or_duplicate_relationships_do_not_retype_a_macro_sheet(tmp_path):
    p = tmp_path / "book.xlsm"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    macro = "http://schemas.microsoft.com/office/2006/relationships/xlMacrosheet"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS_NS}">'
                   '<sheets><sheet name="M" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   f'<Relationships xmlns="{rel_ns}" xmlns:f="urn:foreign">'
                   f'<Relationship Id="rId2" Type="{macro}" Target="m.xml"/>'
                   f'<f:Relationship Id="rId2" Type="{_REL_T}worksheet" Target="w.xml"/>'
                   f'<Relationship Id="rId2" Type="{_REL_T}worksheet" Target="w.xml"/>'
                   '</Relationships>')
    sheets, anomalies = read_sheet_inventory(p)
    assert "unresolved_sheet_relationship" in anomalies  # duplicate Id: ambiguous
    assert sheets[0]["type"] != "worksheet"


def test_nested_alternate_content_is_scanned_linearly(tmp_path):
    import time

    depth = 2000
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    body = (f"<mc:AlternateContent {mc}>" * depth) + ("</mc:AlternateContent>" * depth)
    p = _workbook(tmp_path, body + '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    t0 = time.monotonic()
    read_sheet_inventory(p)
    assert time.monotonic() - t0 < 2.0


def test_convert_reports_reader_anomalies_and_never_verifies(tmp_path):
    meta = _convert(tmp_path, _SheetRunner(3, SHEETS, anomalies=["nonstandard_package_part"]),
                    Limits())
    sh = meta["sheets"]
    assert sh["page_mapping_verified"] is False
    assert sh["reader_anomalies"] == ["nonstandard_package_part"]
    assert all(p.get("sheet_name") is None for p in meta["pages"])


def test_convert_emits_an_unverified_block_when_the_reader_found_no_sheet(tmp_path):
    meta = _convert(tmp_path, _SheetRunner(2, [], anomalies=["unreadable_workbook"]), Limits())
    assert meta["sheets"]["page_mapping_verified"] is False
    assert meta["sheets"]["reader_anomalies"] == ["unreadable_workbook"]


def test_reader_failures_are_anomalies_not_silence(tmp_path):
    p = tmp_path / "book.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml", '<!DOCTYPE workbook><workbook xmlns="' + _MAIN + '">'
                   '<sheets><sheet name="A"/></sheets></workbook>')
    assert read_sheet_inventory(p) == ([], ["unreadable_workbook"])
    q = tmp_path / "sjis.xlsx"
    with zipfile.ZipFile(q, "w") as z:
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="Shift_JIS"?><workbook '
                   f'xmlns="{_MAIN}"><sheets><sheet name="A"/></sheets></workbook>')
    assert read_sheet_inventory(q)[1] == ["unreadable_workbook"]


@pytest.mark.parametrize("rels_target,anomaly", [
    ("xl/real.xml", "nonstandard_package_part"),
    (None, "nonstandard_package_part"),
])
def test_reader_flags_a_package_whose_office_document_is_elsewhere(tmp_path, rels_target, anomaly):
    p = _workbook(tmp_path, '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    with zipfile.ZipFile(p) as z:
        items = {n: z.read(n) for n in z.namelist() if n != "_rels/.rels"}
    with zipfile.ZipFile(p, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
        if rels_target:
            z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                       f'package/2006/relationships"><Relationship Id="rId1" '
                       f'Type="{_REL_T}officeDocument" Target="{rels_target}"/></Relationships>')
    assert anomaly in read_sheet_inventory(p)[1]


@pytest.mark.parametrize("body,anomaly", [
    ('<sheets><sheet name="M" r:id="rId2" s:id="rId1" '
     'xmlns:s="http://purl.oclc.org/ooxml/officeDocument/relationships"/></sheets>',
     "multiple_relationship_ids"),
    ('<sheets><sheet name="A_x0042_C" r:id="rId1"/></sheets>', "escaped_sheet_name"),
    ('<bookViews><workbookView activeTab="0_1"/></bookViews>'
     '<sheets><sheet name="A" r:id="rId1"/></sheets>', "bad_active_tab"),
    ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/'
     '2006"><mc:Choice Requires="x15"><bookViews><workbookView activeTab="0"/></bookViews>'
     '</mc:Choice></mc:AlternateContent><sheets><sheet name="A" r:id="rId1"/></sheets>',
     "markup_compatibility"),
])
def test_reader_flags_more_libreoffice_divergences(tmp_path, body, anomaly):
    assert anomaly in read_sheet_inventory(_workbook(tmp_path, body, rels=True))[1]


@pytest.mark.parametrize("mutate,anomaly", [
    (lambda z: z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet"),
     "odf_polyglot"),
    (lambda z: z.writestr("content.xml", "<office:document-content/>"), "odf_polyglot"),
])
def test_reader_flags_an_odf_polyglot(tmp_path, mutate, anomaly):
    p = _workbook(tmp_path, '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    with zipfile.ZipFile(p, "a") as z:
        mutate(z)
    assert anomaly in read_sheet_inventory(p)[1]


def test_reader_requires_the_content_type_and_exact_office_document_uri(tmp_path):
    p = _workbook(tmp_path, '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    with zipfile.ZipFile(p) as z:
        items = {n: z.read(n) for n in z.namelist() if n != "[Content_Types].xml"}
    with zipfile.ZipFile(p, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    assert "nonstandard_package_part" in read_sheet_inventory(p)[1]
    q = _workbook(tmp_path / "q", '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True) \
        if (tmp_path / "q").mkdir() is None else None
    with zipfile.ZipFile(q) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["_rels/.rels"] = items["_rels/.rels"].replace(
        b"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        b"urn:decoy/officeDocument")
    with zipfile.ZipFile(q, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    assert "nonstandard_package_part" in read_sheet_inventory(q)[1]


def test_mixed_namespace_book_views_are_an_anomaly(tmp_path):
    body = ('<s:bookViews xmlns:s="http://purl.oclc.org/ooxml/spreadsheetml/main">'
            '<s:workbookView activeTab="1"/></s:bookViews>'
            '<sheets><sheet name="A" r:id="rId1"/><sheet name="B" state="hidden" r:id="rId1"/>'
            '</sheets>')
    assert "noncanonical_sheet_element" in read_sheet_inventory(_workbook(tmp_path, body, rels=True))[1]


def test_inventory_source_says_when_type_and_state_came_from_a_reexport(tmp_path):
    runner = _SheetRunner(3, SHEETS)
    orig = runner.convert_to_pdf

    def convert(*a, **k):
        r = orig(*a, **k)
        runner.last_run_sheets_from_reexport = True
        return r

    runner.convert_to_pdf = convert
    meta = _convert(tmp_path, runner, Limits())
    assert meta["sheets"]["inventory_source"] == "libreoffice_xlsx_reexport"


def test_a_workbook_typed_by_the_xml_default_is_not_an_anomaly(tmp_path):
    p = _workbook(tmp_path, '<sheets><sheet name="A" r:id="rId1"/></sheets>', rels=True)
    with zipfile.ZipFile(p) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["[Content_Types].xml"] = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="xml" ContentType="application/vnd.openxmlformats-officedocument.'
        b'spreadsheetml.sheet.main+xml"/></Types>')
    with zipfile.ZipFile(p, "w") as z:
        for n, b in items.items():
            z.writestr(n, b)
    assert read_sheet_inventory(p)[1] == []
