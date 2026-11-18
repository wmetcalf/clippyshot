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

from clippyshot.libreoffice._safexml import safe_fromstring
from clippyshot.libreoffice._safezip import (
    ExtractionBudget,
    ExtractionLimitExceeded,
    bounded_read,
)

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
# Transitional and Strict workbook roots (LibreOffice imports nothing from an unqualified one).
_SPREADSHEETML_NS = (
    _OOXML_NS,
    "{http://purl.oclc.org/ooxml/spreadsheetml/main}",
)
# Sheets kept in the inventory. A workbook with more is reported truncated and its page
# mapping unverified; this bounds the metadata a crafted workbook can make us emit.
MAX_SHEETS = 512


_REL_NS = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}",
    "{http://purl.oclc.org/ooxml/officeDocument/relationships}",
)
_SPEC_STATES = frozenset({"visible", "hidden", "veryHidden"})
# Characters Calc refuses in a sheet name (it renames or drops such a sheet on import).
_CALC_BAD_NAME = re.compile(r"[\[\]*?:/\\]")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _sheet_types_by_rid(zf: zipfile.ZipFile) -> dict[str, str] | None:
    """Map workbook relationship id -> sheet kind, from xl/_rels/workbook.xml.rels.

    An Excel 4 macro sheet is identified by its relationship type (xlMacrosheet), not by
    any attribute on <sheet>. None when the rels part is missing or unparseable.
    """
    try:
        data = bounded_read(zf, "xl/_rels/workbook.xml.rels")
        root = safe_fromstring(data)
    except (KeyError, ExtractionLimitExceeded, ET.ParseError):
        return None
    out: dict[str, str] = {}
    for rel in root.findall(f"{_PKG_REL_NS}Relationship"):
        rid, rtype = rel.attrib.get("Id"), rel.attrib.get("Type", "")
        if not rid:
            continue
        if rid in out:
            out[rid] = "ambiguous"  # duplicate Id: which one LO uses is not ours to guess
            continue
        kind = rtype.rsplit("/", 1)[-1]
        out[rid] = {"worksheet": "worksheet", "chartsheet": "chartsheet",
                    "xlMacrosheet": "macro", "xlIntlMacrosheet": "macro",
                    "dialogsheet": "dialog"}.get(kind, kind[:32] or "unknown")
    return out


def read_sheet_inventory(xlsx_path: Path) -> tuple[list[dict], list[str]]:
    """See :func:`_read_sheet_inventory`; never raises.

    Any parse failure on a zip that does carry ``xl/workbook.xml`` (a DOCTYPE the safe
    parser refuses, a multi-byte encoding expat can't take, ...) is reported as the
    ``unreadable_workbook`` anomaly rather than an empty, anomaly-free result, so the
    converter still emits an unverified sheets block for a workbook LO may import fine.
    """
    try:
        return _read_sheet_inventory(xlsx_path)
    except _Unreadable:
        return [], ["unreadable_workbook"]
    except Exception:  # noqa: BLE001
        try:
            with zipfile.ZipFile(xlsx_path) as zf:
                zf.getinfo("xl/workbook.xml")
        except Exception:  # noqa: BLE001
            return [], []
        return [], ["unreadable_workbook"]


class _Unreadable(Exception):
    pass


_OFFICE_DOC_REL = "/officeDocument"
_ESCAPED_XSTRING = re.compile(r"_x[0-9A-Fa-f]{4}_")


def _package_anomalies(zf: zipfile.ZipFile) -> set[str]:
    """Is xl/workbook.xml the part LibreOffice imports? LO follows _rels/.rels'
    officeDocument relationship, not a fixed path: a package pointing elsewhere (or with a
    case-variant twin of the part) can show this reader a decoy workbook."""
    out: set[str] = set()
    names = zf.namelist()
    if sum(1 for n in names if n.lower() == "xl/workbook.xml") != 1:
        out.add("nonstandard_package_part")
    try:
        root = safe_fromstring(bounded_read(zf, "_rels/.rels"))
    except Exception:  # noqa: BLE001
        out.add("nonstandard_package_part")
        return out
    targets = [r.attrib.get("Target", "") for r in root.findall(f"{_PKG_REL_NS}Relationship")
               if r.attrib.get("Type", "").endswith(_OFFICE_DOC_REL)]
    if len(targets) != 1 or targets[0] not in ("xl/workbook.xml", "/xl/workbook.xml"):
        out.add("nonstandard_package_part")
    return out


def _read_sheet_inventory(xlsx_path: Path) -> tuple[list[dict], list[str]]:
    """Parse ``xl/workbook.xml``: (per-sheet metadata in workbook order, anomalies).

    Each entry is ``{name, state, type, active}``; ``type`` comes from the sheet's workbook
    relationship (``macro`` for an Excel 4 / XLM macro sheet). ``active`` marks the
    workbookView activeTab, which LibreOffice forces visible on import.

    ``anomalies`` lists every way this workbook could make this reader's list differ from
    the sheets LibreOffice actually imports (unnamed sheets LO auto-names, names Calc
    rejects, duplicates, non-spec states, markup-compatibility wrappers, foreign or mixed
    namespaces, several <sheets> blocks, an unqualified root LO does not import at all,
    unresolvable relationships). Callers must not trust a page-count match as a positional
    sheet mapping unless it is empty. The list is a lenient superset so a hidden sheet in an
    odd position is still reported.

    Returns ``([], [])`` if the file is not an OOXML zip, workbook.xml is missing, or the
    XML fails to parse. Never raises on a well-formed but unusual zip.
    """
    if not zipfile.is_zipfile(xlsx_path):
        return [], []
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            try:
                data = bounded_read(zf, "xl/workbook.xml")
            except KeyError:
                return [], []
            except ExtractionLimitExceeded:
                raise _Unreadable from None
            rel_types = _sheet_types_by_rid(zf)
            package = _package_anomalies(zf)
    except (zipfile.BadZipFile, OSError):
        return [], []
    try:
        root = safe_fromstring(data)
    except Exception:  # noqa: BLE001 — DOCTYPE refused, unsupported encoding, malformed
        raise _Unreadable from None
    anomalies: set[str] = set(package)
    root_ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    if root_ns not in _SPREADSHEETML_NS or _local(root.tag) != "workbook":
        anomalies.add("unsupported_root")
    if rel_types is None:
        anomalies.add("no_workbook_rels")
    active_tab = 0
    view = root.find(f"{root_ns}bookViews/{root_ns}workbookView")
    if view is not None:
        raw_tab = view.attrib.get("activeTab", "0")
        if re.fullmatch(r"[0-9]{1,9}", raw_tab):
            active_tab = int(raw_tab)
        else:
            anomalies.add("bad_active_tab")  # LO's toInt32 reads '0_1' etc. differently
    canonical_blocks = root.findall(f"{root_ns}sheets")
    if len(canonical_blocks) > 1:
        anomalies.add("multiple_sheets_blocks")
    canonical = {id(e) for b in canonical_blocks for e in b if e.tag == f"{root_ns}sheet"}
    out: list[dict] = []
    seen: set[str] = set()
    # One linear pass, document order, carrying "inside mc:AlternateContent". Excel routinely
    # wraps unrelated workbook.xml parts (x15ac:absPath, ...) in AlternateContent; only one
    # that carries sheets changes what LO imports.
    stack: list[tuple[ET.Element, bool]] = [(root, False)]
    ordered: list[ET.Element] = []
    while stack:
        node, in_ac = stack.pop()
        local = _local(node.tag)
        in_ac = in_ac or local == "AlternateContent"
        if local in ("sheet", "sheets", "bookViews", "workbookView"):
            if in_ac:
                anomalies.add("markup_compatibility")
            if local == "sheet":
                ordered.append(node)
        stack.extend((child, in_ac) for child in reversed(list(node)))
    for el in ordered:
        if id(el) not in canonical:
            anomalies.add("noncanonical_sheet_element")
        if len(out) > MAX_SHEETS:
            continue  # the caller sees > MAX_SHEETS and marks the list truncated
        name = el.attrib.get("name")
        if not isinstance(name, str) or not name:
            anomalies.add("unnamed_sheet")
            continue
        if _CALC_BAD_NAME.search(name) or name.startswith("'") or name.endswith("'"):
            anomalies.add("invalid_sheet_name")
        if _ESCAPED_XSTRING.search(name):
            anomalies.add("escaped_sheet_name")  # LO decodes _xHHHH_; the names differ
        if name.casefold() in seen:
            anomalies.add("duplicate_sheet_name")
        seen.add(name.casefold())
        state = el.attrib.get("state", "visible")
        if state not in _SPEC_STATES:
            anomalies.add("nonstandard_sheet_state")
        rids = [el.attrib[k + "id"] for k in _REL_NS if k + "id" in el.attrib]
        if len(rids) > 1:
            anomalies.add("multiple_relationship_ids")  # LO takes the first in document order
        rid = rids[0] if rids else None
        if rel_types is not None and (rid is None or rid not in rel_types
                                      or rel_types[rid] == "ambiguous"):
            anomalies.add("unresolved_sheet_relationship")
        sheet_type = (rel_types or {}).get(rid or "") or el.attrib.get("type", "worksheet")
        out.append({"name": name, "state": state, "type": sheet_type,
                    "active": len(out) == active_tab})
    if out and not 0 <= active_tab < len(out):
        anomalies.add("bad_active_tab")  # LO clamps or ignores it; we can't say which tab
    return out, sorted(anomalies)


def read_sheet_list(xlsx_path: Path) -> list[dict]:
    """The sheet list of :func:`read_sheet_inventory`, without the anomalies."""
    return read_sheet_inventory(xlsx_path)[0]


def visible_sheet_names(sheets: list[dict]) -> list[str]:
    """Visible non-macro names for callers explicitly filtering workbook inventory.

    This is NOT the SinglePageSheets PDF order: that mode includes hidden sheets.
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
            budget = ExtractionBudget()
            for name in zf.namelist():
                if not re.match(r"xl/worksheets/sheet\d+\.xml$", name):
                    continue
                data = budget.read(zf, name)
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

    budget = ExtractionBudget()
    with zipfile.ZipFile(buf, "r") as zin:
        names = zin.namelist()
        for name in names:
            # Bounded per-entry + cumulative read: a single worksheet that
            # decompresses to gigabytes (or thousands of padded entries)
            # raises ExtractionLimitExceeded, which the caller catches and
            # routes to the sandboxed two-pass conversion instead of OOMing.
            data = budget.read(zin, name)
            # Worksheets live under xl/worksheets/sheet*.xml
            if re.match(r"xl/worksheets/sheet\d+\.xml$", name):
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
