"""Pre-processing for spreadsheet files: set landscape + fit-to-page on every sheet.

For OOXML files (.xlsx / .xlsm) we patch the XML in place.
For binary formats (.xls / .xlsb) we do a two-pass conversion:
  1. soffice converts the binary file → .xlsx (in a temp dir)
  2. We patch the .xlsx
  3. The caller then converts the patched .xlsx → PDF

The public entry point is ``patch_ooxml_for_print(path)`` which modifies the
zip in-place and is a no-op if the file is not a recognised OOXML spreadsheet.
``is_spreadsheet_extension(suffix)`` tells callers whether the two-pass path
is needed.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# OOXML suffixes we can patch directly (zip+xml)
_OOXML_SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xlsm"})

# Binary suffixes that need a two-pass (convert-to-xlsx first)
_BINARY_SPREADSHEET_SUFFIXES = frozenset({".xls", ".xlsb"})

SPREADSHEET_SUFFIXES = _OOXML_SPREADSHEET_SUFFIXES | _BINARY_SPREADSHEET_SUFFIXES


def is_spreadsheet(path: Path) -> bool:
    return path.suffix.lower() in SPREADSHEET_SUFFIXES


# OOXML spreadsheet namespace used in xl/workbook.xml. Hardcoded because
# this namespace is frozen in the OPC/OOXML spec (ECMA-376 part 1).
_OOXML_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_sheet_list(xlsx_path: Path) -> list[dict]:
    """Parse ``xl/workbook.xml`` and return per-sheet metadata in workbook order.

    Each entry is a dict with:

    - ``name``: human-readable sheet name (up to 31 chars per Excel, uncapped here)
    - ``state``: ``"visible"`` / ``"hidden"`` / ``"veryHidden"`` (defaults to
      ``"visible"`` when the attribute is absent)
    - ``type``: ``"worksheet"`` / ``"chartsheet"`` / ``"macro"`` / ``"dialog"``
      (defaults to ``"worksheet"``; the ``type`` attribute only appears for
      non-default types, and Excel 4 macro sheets set it to ``"macro"``)

    Returns an empty list if the file is not an OOXML zip, the workbook.xml
    is missing, or the XML fails to parse. This function is read-only and
    safe to call on any Path — it never raises on a well-formed but unusual
    zip (encrypted content, foreign namespaces, missing sheet nodes, etc.).
    """
    if not zipfile.is_zipfile(xlsx_path):
        return []
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            try:
                data = zf.read("xl/workbook.xml")
            except KeyError:
                return []
    except (zipfile.BadZipFile, OSError):
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    sheets = root.find(f"{_OOXML_NS}sheets")
    if sheets is None:
        # Some non-Microsoft writers emit without a namespace; try unqualified.
        sheets = root.find("sheets")
    if sheets is None:
        return []
    out: list[dict] = []
    for sheet in sheets:
        # Accept both namespaced and bare tag names.
        tag = sheet.tag.rsplit("}", 1)[-1]
        if tag != "sheet":
            continue
        name = sheet.attrib.get("name")
        if not isinstance(name, str) or not name:
            continue
        state = sheet.attrib.get("state", "visible")
        # type attribute: when missing it's a worksheet by convention. Only
        # Excel 4 macro sheets set this explicitly.
        sheet_type = sheet.attrib.get("type", "worksheet")
        out.append({"name": name, "state": state, "type": sheet_type})
    return out


def visible_sheet_names(sheets: list[dict]) -> list[str]:
    """Return names of sheets LibreOffice will emit as PDF pages.

    LO Calc's PDF export skips ``hidden`` and ``veryHidden`` sheets, and
    (for ``SinglePageSheets=true``) also drops Excel 4 macro sheets since
    they can't meaningfully render one-per-page. Output order matches the
    input workbook order so the i-th name aligns with PDF page i.
    """
    return [
        s["name"]
        for s in sheets
        if s.get("state", "visible") == "visible" and s.get("type", "worksheet") != "macro"
    ]


# Maximum "reasonable" used range for SinglePageSheets.
# Beyond this, content gets squished unreadably small.
_MAX_REASONABLE_COLS = 30   # ~column AD
_MAX_REASONABLE_ROWS = 200

def has_oversized_range(path: Path) -> bool:
    """READ-ONLY check: does any sheet have an unreasonably large used range?

    Handles both OOXML (.xlsx/.xlsm — parses dimension XML) and OLE binary
    (.xls/.xlsb — checks Workbook stream size via olefile). Never modifies
    the file.
    """
    # Try OOXML first
    if _check_ooxml_dimensions(path):
        return True
    # Try OLE binary
    if _check_ole_workbook_size(path):
        return True
    return False


def _check_ooxml_dimensions(path: Path) -> bool:
    """Check OOXML spreadsheet dimension elements for oversized ranges."""
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                    continue
                data = zf.read(name)
                m = re.search(rb'<dimension\s+ref="([^"]+)"', data)
                if not m:
                    continue
                ref = m.group(1).decode("ascii", errors="ignore")
                parts = ref.split(":")
                if len(parts) != 2:
                    continue
                end_cell = parts[1]
                col_str = "".join(c for c in end_cell if c.isalpha())
                row_str = "".join(c for c in end_cell if c.isdigit())
                if not col_str or not row_str:
                    continue
                if _col_to_num(col_str) > _MAX_REASONABLE_COLS or int(row_str) > _MAX_REASONABLE_ROWS:
                    return True
    except Exception:
        pass
    return False


def _check_ole_workbook_size(path: Path) -> bool:
    """Check OLE spreadsheets for oversized Workbook streams.

    Binary xls/xlsb files store sheet data in the Workbook or Book stream.
    A large stream (>500KB) typically indicates many rows/columns which
    would squish badly under SinglePageSheets. This is a rough heuristic
    but catches the common malware case.
    """
    try:
        import olefile
        if not olefile.isOleFile(str(path)):
            return False
        ole = olefile.OleFileIO(str(path))
        for stream_name in ("Workbook", "Book"):
            if ole.exists(stream_name):
                size = ole.get_size(stream_name)
                ole.close()
                return size > 500_000  # >500KB workbook → likely oversized
        ole.close()
    except Exception:
        pass
    return False


def _col_to_num(col: str) -> int:
    """Convert Excel column letters to a 1-based number (A=1, Z=26, AA=27)."""
    n = 0
    for c in col.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n


def needs_binary_conversion(path: Path) -> bool:
    """True if the file must be converted to xlsx before we can patch it."""
    return path.suffix.lower() in _BINARY_SPREADSHEET_SUFFIXES


def patch_ooxml_for_print(path: Path) -> None:
    """Patch every worksheet in an OOXML spreadsheet zip for landscape + fit-to-1-page.

    Modifies the file in-place. Safe to call on non-OOXML files — it will just
    raise ``zipfile.BadZipFile`` which the caller can ignore or handle.
    """
    if not zipfile.is_zipfile(path):
        return  # not a zip, skip silently

    original = path.read_bytes()
    buf = io.BytesIO(original)

    patched_entries: dict[str, bytes] = {}

    with zipfile.ZipFile(buf, "r") as zin:
        names = zin.namelist()
        for name in names:
            data = zin.read(name)
            # Worksheets live under xl/worksheets/sheet*.xml
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name) or \
               name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                data = _patch_worksheet_xml(data)
            patched_entries[name] = data

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        # Preserve the original zip info for most entries (metadata, etc.)
        buf.seek(0)
        with zipfile.ZipFile(buf, "r") as zin_meta:
            for info in zin_meta.infolist():
                zout.writestr(info, patched_entries[info.filename])

    path.write_bytes(out_buf.getvalue())


# ---------------------------------------------------------------------------
# XML patching helpers
# ---------------------------------------------------------------------------

# Page setup: landscape orientation. We DON'T set fitToPage here because
# the actual scaling is handled by SinglePageSheets in the PDF export
# filter, which dynamically sizes each sheet to fit. The patch just
# ensures landscape orientation so wide spreadsheets render naturally.
_PAGE_SETUP_ATTRS = (
    'orientation="landscape" '
)

# sheetFormatPr: set default row/column outline so content isn't tiny
_SHEET_FORMAT_RE = re.compile(
    rb'(<sheetFormatPr\b[^/]*/?>)', re.IGNORECASE
)

_PAGE_SETUP_RE = re.compile(
    rb'<pageSetup\b[^/]*/?>|<pageSetup\b.*?</pageSetup>',
    re.IGNORECASE | re.DOTALL,
)

_SHEET_VIEW_FIT_TO_PAGE_RE = re.compile(
    rb'(<sheetView\b[^>]*?>)',
    re.IGNORECASE,
)

_PRINT_OPTIONS_RE = re.compile(
    rb'(<printOptions\b[^/]*/?>)',
    re.IGNORECASE,
)

# Where to insert pageSetup if it's not already there — just before </worksheet>
_WORKSHEET_CLOSE_RE = re.compile(rb'</worksheet>', re.IGNORECASE)


def _patch_worksheet_xml(data: bytes) -> bytes:
    """Patch a single xl/worksheets/sheet*.xml to landscape + fit-to-1-page."""

    # Set landscape orientation. SinglePageSheets in the PDF export filter
    # handles the actual fit-to-page scaling; we just need landscape so
    # wide spreadsheets render naturally instead of portrait-squished.
    new_page_setup = b'<pageSetup orientation="landscape"/>'

    if _PAGE_SETUP_RE.search(data):
        data = _PAGE_SETUP_RE.sub(new_page_setup, data)
    else:
        data = _WORKSHEET_CLOSE_RE.sub(
            new_page_setup + b'</worksheet>', data, count=1
        )

    return data
