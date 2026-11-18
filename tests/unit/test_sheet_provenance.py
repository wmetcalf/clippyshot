import json
import zipfile
from pathlib import Path

from PIL import Image

from clippyshot.converter import Converter, ConvertOptions, _page_sheet_names, _sheet_provenance
from clippyshot.libreoffice.sheet_prep import MAX_SHEETS, read_sheet_list
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


def _workbook(tmp_path, body: str, name: str = "book.xlsx") -> Path:
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{_MAIN}">{body}</workbook>')
    return p


def test_reader_matches_libreoffice_on_foreign_and_repeated_sheet_blocks(tmp_path):
    """LO ignores a foreign-namespace <x:sheet> and imports every <sheets> block; a reader
    that differs lets a same-length but different list pass as a verified mapping."""
    p = _workbook(tmp_path,
                  '<sheets><sheet name="A"/><x:sheet xmlns:x="urn:not-spreadsheetml" name="Decoy"/>'
                  '<sheet name="B"/></sheets><sheets><sheet name="PAYLOAD" state="hidden"/></sheets>')
    assert [(s["name"], s["state"]) for s in read_sheet_list(p)] == [
        ("A", "visible"), ("B", "visible"), ("PAYLOAD", "hidden")]


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
    def __init__(self, pages, sheets, single=True, export_filter="calc_pdf_Export"):
        self.pages, self.sheets, self.single, self.filter = pages, sheets, single, export_filter
        self.last_run_sheets: list = []
        self.last_run_single_page_sheets = False
        self.last_run_export_filter = ""

    def convert_to_pdf(self, input_path, output_dir, limits, label="xlsx"):
        out = Path(output_dir) / "input.pdf"
        imgs = [Image.new("RGB", (612, 792), (255, 255, 255)) for _ in range(self.pages)]
        imgs[0].save(out, "PDF", save_all=True, append_images=imgs[1:], resolution=72.0)
        self.last_run_sheets = list(self.sheets)
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
