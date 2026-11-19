"""Sheet type/state straight from a binary workbook's own records (xls / xlsb).

xls/xlsb inputs reach LibreOffice's PDF export through a two-pass xlsx re-export, and the
sheet inventory used to be read from that re-export. LO writes an Excel 4 (XLM) macro sheet
there as a plain worksheet and a ``veryHidden`` sheet as ``hidden`` -- exactly the two
attributes that matter for macro malware. This module reads them from the ORIGINAL file:

* xls (BIFF8, the ``Workbook`` stream of the OLE container): the BoundSheet8 records of the
  workbook-globals substream -- ``hsState`` (0 visible, 1 hidden, 2 very hidden) and ``dt``
  (0 worksheet or dialog, 1 XLM macro sheet, 2 chart, 6 VBA module).
* xlsb: the BrtBundleSh records of ``xl/workbook.bin`` (``hsState`` + name + relationship
  id); the sheet type comes from that relationship in ``xl/_rels/workbook.bin.rels``
  (worksheet / chartsheet / xlMacrosheet / dialogsheet), as for xlsx.

Everything here is bounded (bytes read, records walked, sheets kept) and returns ``None``
instead of raising on anything it does not fully understand -- an encrypted xls (FILEPASS:
BoundSheet8 names and flags are RC4-encrypted), BIFF5 and older, a truncated record, an
unknown state -- so the caller falls back to the re-export inventory. Parsing runs on the
untrusted input outside the sandbox, like the rest of sheet_prep: no recursion, no
decompression beyond the zip budget, no allocation driven by a length field beyond the
bounded buffer.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

# Sheets kept before giving up. Matches sheet_prep.MAX_SHEETS headroom: a workbook with more
# than this many sheet records is not aligned (the caller keeps the re-export inventory).
MAX_RECORD_SHEETS = 4096
# Bytes of the BIFF Workbook stream walked looking for the globals substream's BoundSheet8
# records. They precede every sheet substream, so a real workbook has them within the first
# few hundred KiB; a globals substream longer than this is treated as unparseable.
MAX_BIFF_GLOBALS_BYTES = 8 << 20
# olefile materialises a whole stream on open; decline a Workbook stream larger than this
# rather than load it (the re-export inventory still stands).
MAX_BIFF_STREAM_BYTES = 32 << 20
# xlsb package parts read in-process (workbook.bin, the rels and [Content_Types].xml) are
# KB-sized in real files; a bigger one is declined rather than parsed into a huge tree.
MAX_XLSB_PART_BYTES = 4 << 20
# Records walked in either format before giving up.
MAX_RECORDS = 200_000

_STATES = {0: "visible", 1: "hidden", 2: "veryHidden"}
# BoundSheet8.dt (0 worksheet, 1 macro, 2 chart, 6 module) is NOT what LibreOffice uses: it
# types each sheet by the BOF that opens its substream at lbPlyPos. Droppers declare XLM
# macro sheets as dt=0 worksheets; read the substream BOF instead.
_BOF_TYPES = {0x0010: "worksheet", 0x0020: "chartsheet", 0x0040: "macro",
              0x0006: "vba_module"}

# BIFF8 record ids.
_BOF = 0x0809
_EOF = 0x000A
_BOUNDSHEET = 0x0085
_FILEPASS = 0x002F
_BIFF8_VERSION = 0x0600

# XLSB record ids (MS-XLSB 2.3).
_BRT_BEGIN_BOOK = 131
_BRT_BEGIN_BUNDLE_SHS = 143
_BRT_BUNDLE_SH = 156
_BRT_END_BUNDLE_SHS = 144

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

XLS_SOURCE = "input_xls_boundsheet_records"
XLSB_SOURCE = "input_xlsb_workbook_records"


class _Bad(Exception):
    pass


class _Declined(Exception):
    """This reader will not vouch for the file's records; ``args[0]`` is the reason."""


# Why the records were not used (sheets.input_records when inventory_source stays the
# re-export). "not_binary_workbook" (ods/csv/...) is not reported.
RECORD_STATUSES = frozenset({"encrypted", "too_large", "unreadable", "unaligned"})


def read_binary_sheet_records(path: Path) -> tuple[str, list[dict]] | None:
    """``(inventory_source, [{name, state, type}, ...])`` in workbook order, or ``None``.

    ``type`` is ``worksheet`` / ``macro`` (XLM) / ``chartsheet`` / ``vba_module`` /
    ``dialog`` (xlsb); ``state`` is ``visible`` / ``hidden`` / ``veryHidden``. Never raises.
    """
    got, _ = read_binary_sheet_records_status(path)
    return got


def read_binary_sheet_records_status(
    path: Path,
) -> tuple[tuple[str, list[dict]] | None, str]:
    """Like :func:`read_binary_sheet_records`, plus why there is no result.

    Status: ``ok``, ``not_binary_workbook``, or one of :data:`RECORD_STATUSES`.
    """
    try:
        # Dispatch on the leading signature, never zipfile.is_zipfile(): that also accepts
        # a zip END record anywhere in the last 64 KiB, which Excel 2007+ .xls files carry
        # inside their Workbook stream (THEME / MSODRAWING), and which an attacker can
        # append to an OLE workbook LibreOffice still imports as xls.
        with open(path, "rb") as fh:
            head = fh.read(8)
        if head == _OLE_MAGIC:
            return (XLS_SOURCE, _ok(_read_xls(path))), "ok"
        if head[:2] == b"\x09\x08":  # a BIFF8 BOF record: bare stream, no CFB container
            return (XLS_SOURCE, _read_raw_biff(path)), "ok"
        if head[:4] == b"PK\x03\x04":
            sheets = _read_xlsb(path)
            if sheets is None:
                return None, "not_binary_workbook"  # a zip whose main part is not .bin
            return (XLSB_SOURCE, sheets), "ok"
        return None, "not_binary_workbook"
    except _Declined as exc:
        return None, str(exc.args[0])
    except Exception:  # noqa: BLE001 - best effort; the caller keeps the re-export inventory
        return None, "unreadable"


def _ok(sheets: list[dict] | None) -> list[dict]:
    if sheets is None:
        raise _Declined("unreadable")
    return sheets


# ─── xls (BIFF8) ─────────────────────────────────────────────────────────────


def _check_cfb_header(path: Path) -> None:
    """Refuse a CFB olefile would do unbounded work on (ole_guard: header counts absurd for
    the file, a directory chain past MAX_OLE_DIR_ENTRIES slots or looping), a FAT count
    absurd for the file even without a DIFAT, or
    whose mini-stream cutoff olefile silently normalises to 4096 while LibreOffice honours
    it (the two would then read different bytes for the same stream)."""
    from clippyshot.ole_guard import OleGuardError, check_cfb

    path = Path(path)
    try:
        check_cfb(path)
    except OleGuardError as exc:
        raise _Declined(exc.reason) from None
    with open(path, "rb") as fh:
        h = fh.read(512)
    (n_fat,) = struct.unpack_from("<I", h, 0x2C)
    (cutoff,) = struct.unpack_from("<I", h, 0x38)
    if cutoff != 4096:
        raise _Declined("unreadable")
    # Stricter than the shared guard (which bounds n_fat only with a DIFAT): a FAT count
    # absurd for the file's size is not a header these records are read from.
    sector = 1 << struct.unpack_from("<H", h, 0x1E)[0]
    sectors = -(-max(0, path.stat().st_size - sector) // sector)
    if n_fat > 2 * (sectors // (sector // 4) + 1) + 16:
        raise _Declined("unreadable")


def _stream_entry(ole, lo_name: str):
    """The root-level entry both olefile and LibreOffice resolve ``lo_name`` to, or None.

    LibreOffice compares names ASCII-uppercased (CFB); olefile lowercases with Unicode
    rules (so the Kelvin sign K matches "k"). Only a name both views agree on, exactly
    once, is trusted. Root level only: an embedded workbook object keeps its own streams
    in a sub-storage, which LibreOffice does not import as this document.
    """
    ole_view = [e for e in ole.root.kids if e.name.lower() == lo_name.lower()]
    lo_view = [e for e in ole.root.kids
               if e.name.isascii() and e.name.upper() == lo_name.upper()]
    if not ole_view and not lo_view:
        return None
    if len(ole_view) != 1 or len(lo_view) != 1 or ole_view[0] is not lo_view[0]:
        raise _Declined("unreadable")
    if ole_view[0].entry_type != 2:  # STGTY_STREAM; a storage by that name is no stream
        raise _Declined("unreadable")
    return ole_view[0]


def _open_capped(ole, entry, limit: int = MAX_BIFF_STREAM_BYTES) -> bytes:
    size = entry.size
    if size > MAX_BIFF_STREAM_BYTES:
        raise _Declined("too_large")
    if size < ole.minisectorcutoff:
        # A small stream lives in the ministream: olefile loads the whole MiniFAT and
        # ministream (sizes from the header / root entry, attacker-chosen) to read it.
        if (ole.root.size > MAX_BIFF_STREAM_BYTES
                or ole.num_mini_fat_sectors * ole.sectorsize > MAX_BIFF_STREAM_BYTES):
            raise _Declined("too_large")
    with ole.openstream([entry.name]) as stream:
        return bytes(stream.read(limit))


def _read_xls(path: Path) -> list[dict] | None:
    import olefile

    if not olefile.isOleFile(str(path)):
        raise _Declined("unreadable")
    _check_cfb_header(path)
    from clippyshot.ole_guard import _bounded_olefile_class, serious_parsing_issues

    with _bounded_olefile_class()(str(path)) as ole:
        # A directory olefile had to repair (an entry linked twice, a sibling link to the
        # root, ...) is one LibreOffice may build differently: not ours to interpret.
        if serious_parsing_issues(ole):
            raise _Declined("unreadable")
        workbook = _stream_entry(ole, "Workbook")
        if workbook is None:
            raise _Declined("unreadable")  # BIFF5 "Book" only, or no workbook at all
        book = _stream_entry(ole, "Book")
        if book is not None:
            # LibreOffice imports Workbook only when its BIFF version is strictly higher
            # than Book's: a BIFF8 BOF in Book ties, and Book then wins.
            head = _open_capped(ole, book, 6)
            if len(head) == 6:
                rid, _rlen, vers = struct.unpack_from("<HHH", head, 0)
                if rid == _BOF and vers >= _BIFF8_VERSION:
                    raise _Declined("unreadable")
        data = _open_capped(ole, workbook)
    return _parse_biff8_stream(data)


def _read_raw_biff(path: Path) -> list[dict]:
    """A bare BIFF8 stream with no CFB container, which LibreOffice also imports."""
    if path.stat().st_size > MAX_BIFF_STREAM_BYTES:
        raise _Declined("too_large")
    return _parse_biff8_stream(path.read_bytes())


def _parse_biff8_stream(data: bytes) -> list[dict]:
    try:
        sheets = parse_biff8_globals(data[:MAX_BIFF_GLOBALS_BYTES])
        if sheets is None:
            raise _Declined("unreadable")
        out = []
        for sh in sheets:
            kind = _substream_type(data, sh["offset"])
            # LibreOffice renders by the substream BOF, but a BoundSheet8 that declares an
            # XLM macro sheet is itself the signal this inventory exists to keep.
            if sh["declared_macro"]:
                kind = "macro"
            out.append({"name": sh["name"], "state": sh["state"], "type": kind})
        return out
    except _Bad:
        raise _Declined("unreadable") from None


def _substream_type(data: bytes, offset: int) -> str:
    """The sheet type LibreOffice uses: the dt of the BIFF8 BOF opening the substream."""
    if offset + 8 > len(data):
        raise _Bad("substream offset outside the stream")
    rtype, rlen = struct.unpack_from("<HH", data, offset)
    if rtype != _BOF or not 4 <= rlen <= 16 or offset + 4 + rlen > len(data):
        raise _Bad("no BOF at substream offset")
    vers, dt = struct.unpack_from("<HH", data, offset + 4)
    if vers != _BIFF8_VERSION or dt not in _BOF_TYPES:
        raise _Bad("unknown substream type")
    return _BOF_TYPES[dt]


def parse_biff8_globals(data: bytes) -> list[dict] | None:
    """BoundSheet8 records of a BIFF8 workbook-globals substream, or ``None``.

    Each entry is ``{name, state, offset}`` (``offset`` = lbPlyPos, the stream offset of
    the sheet's substream BOF). Raises ``_Bad`` on a malformed stream; returns ``None``
    for one this reader declines (not BIFF8, globals not terminated within the bytes
    given) and raises ``_Declined("encrypted")`` on FILEPASS.
    """
    pos = 0
    n = len(data)
    sheets: list[dict] = []
    first = True
    for _ in range(MAX_RECORDS):
        if pos + 4 > n:
            return None  # globals substream not terminated within the bytes read
        rtype, rlen = struct.unpack_from("<HH", data, pos)
        body = data[pos + 4: pos + 4 + rlen]
        if len(body) != rlen:
            raise _Bad("truncated record")
        pos += 4 + rlen
        if first:
            # Must open with a BIFF8 workbook-globals BOF (vers 0x0600, dt 0x0005).
            # LibreOffice's DetectBiffVersion only recognises a BOF of 4..16 bytes.
            if rtype != _BOF or not 4 <= rlen <= 16:
                return None
            vers, dt = struct.unpack_from("<HH", body, 0)
            if vers != _BIFF8_VERSION or dt != 0x0005:
                return None
            first = False
            continue
        if rtype == _FILEPASS:
            # BoundSheet8 names/flags are RC4-encrypted after this.
            raise _Declined("encrypted")
        if rtype == _BOF:
            raise _Bad("nested BOF inside globals")
        if rtype == _EOF:
            return sheets
        if rtype != _BOUNDSHEET:
            continue
        if len(sheets) >= MAX_RECORD_SHEETS:
            return None
        sheets.append(_boundsheet(body))
    return None


def _boundsheet(body: bytes) -> dict:
    # lbPlyPos (4) | hsState (1: low 2 bits; the rest reserved) | dt (1, ignored: LO types
    # sheets by the substream BOF) | stName
    if len(body) < 8:
        raise _Bad("short BoundSheet8")
    (offset,) = struct.unpack_from("<I", body, 0)
    hs = body[4] & 0x03
    if hs not in _STATES:
        raise _Bad("unknown BoundSheet8 state")
    cch = body[6]
    high = body[7] & 0x01
    raw = body[8:]
    need = cch * (2 if high else 1)
    if cch == 0 or len(raw) < need:
        raise _Bad("bad BoundSheet8 name")
    # ShortXLUnicodeString: fHighByte=0 means each char is the low byte of a UTF-16 unit.
    name = raw[:need].decode("utf-16-le" if high else "latin-1", errors="replace")
    return {"name": name, "state": _STATES[hs], "offset": offset,
            "declared_macro": body[5] == 1}


# ─── xlsb ────────────────────────────────────────────────────────────────────


_XLSB_WORKBOOK_CT = "application/vnd.ms-excel.sheet.binary.macroEnabled.main"


def _xlsb_workbook_part(zf: zipfile.ZipFile) -> str | None:
    """The binary workbook part LibreOffice imports from this package, or ``None`` when
    the package's main part is not a BIFF12 workbook (an xlsx, an ods, ...).

    LibreOffice follows ``_rels/.rels``' officeDocument relationship (exact URI) to any
    part name and parses it as BIFF12 when the name ends in ``.bin``; the part's content
    type must be the xlsb main type. Anything ambiguous raises ``_Declined``: a package
    could otherwise show this reader a decoy part (codex on #68).
    """
    from clippyshot.libreoffice._safexml import safe_fromstring
    from clippyshot.libreoffice._safezip import bounded_read
    from clippyshot.libreoffice.sheet_prep import (
        _CT_NS, _OFFICE_DOC_REL, _OFFICE_DOC_TYPES, _PKG_REL_NS,
    )

    try:
        rels = safe_fromstring(bounded_read(zf, "_rels/.rels", MAX_XLSB_PART_BYTES)).findall(
            f"{_PKG_REL_NS}Relationship")
        ct = safe_fromstring(bounded_read(zf, "[Content_Types].xml", MAX_XLSB_PART_BYTES))
    except KeyError:
        return None
    except Exception:  # noqa: BLE001
        raise _Declined("unreadable") from None
    if any(r.attrib.get("Type", "").endswith(_OFFICE_DOC_REL)
           and r.attrib.get("Type") not in _OFFICE_DOC_TYPES for r in rels):
        raise _Declined("unreadable")
    targets = [r.attrib.get("Target", "") for r in rels
               if r.attrib.get("Type") in _OFFICE_DOC_TYPES]
    if len(targets) != 1:
        raise _Declined("unreadable")
    part = targets[0].lstrip("/")
    if not part.lower().endswith(".bin"):
        return None  # an xlsx/xlsm main part: not a binary workbook
    if "\\" in part or ".." in part.split("/") or not part:
        raise _Declined("unreadable")
    members = [n for n in zf.namelist() if n.lower() == part.lower()]
    if len(members) != 1:
        raise _Declined("unreadable")  # missing, or a case-variant twin LO might read
    # OPC part names compare case-insensitively.
    overrides = [o for o in ct.findall(f"{_CT_NS}Override")
                 if o.attrib.get("PartName", "").lower() == "/" + part.lower()]
    if overrides:
        ok = all(o.attrib.get("ContentType", "") == _XLSB_WORKBOOK_CT for o in overrides)
    else:
        # LibreOffice's OOXML detector matches the Default extension case-sensitively.
        ok = any(d.attrib.get("Extension", "") == "bin"
                 and d.attrib.get("ContentType", "") == _XLSB_WORKBOOK_CT
                 for d in ct.findall(f"{_CT_NS}Default"))
    if not ok:
        raise _Declined("unreadable")
    return members[0]  # the zip member's own spelling (zipfile lookups are case-exact)


def _read_xlsb(path: Path) -> list[dict] | None:
    """The xlsb sheet records; ``None`` when the package's main part is not a BIFF12
    workbook; raises ``_Declined`` for an xlsb this reader will not vouch for."""
    # Late import: sheet_prep imports this module.
    from clippyshot.libreoffice._safezip import ExtractionLimitExceeded, bounded_read
    from clippyshot.libreoffice.sheet_prep import _sheet_types_by_rid

    with zipfile.ZipFile(path) as zf:
        part = _xlsb_workbook_part(zf)
        if part is None:
            return None
        folder, _, name = part.rpartition("/")
        rels_part = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"
        try:
            data = bounded_read(zf, part, MAX_XLSB_PART_BYTES)
        except ExtractionLimitExceeded:
            raise _Declined("too_large") from None
        rel_types = _sheet_types_by_rid(zf, rels_part, MAX_XLSB_PART_BYTES)
    if rel_types is None:
        raise _Declined("unreadable")
    try:
        records = parse_xlsb_bundle_sheets(data)
    except _Bad:
        raise _Declined("unreadable") from None
    if records is None:
        raise _Declined("unreadable")
    out: list[dict] = []
    for name, state, rid in records:
        if rid is None:
            out.append({"name": name, "state": state, "type": "vba_module"})
            continue
        kind = rel_types.get(rid)
        if kind is None or kind == "ambiguous":
            raise _Declined("unreadable")
        out.append({"name": name, "state": state, "type": kind})
    return out


def _varint(data: bytes, pos: int, max_bytes: int) -> tuple[int, int]:
    value = 0
    for i in range(max_bytes):
        if pos >= len(data):
            raise _Bad("truncated record header")
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return value, pos
    raise _Bad("overlong record header")


def _wide_string(body: bytes, pos: int, *, nullable: bool) -> tuple[str | None, int]:
    if pos + 4 > len(body):
        raise _Bad("truncated string length")
    (cch,) = struct.unpack_from("<I", body, pos)
    pos += 4
    if nullable and cch == 0xFFFFFFFF:
        return None, pos
    end = pos + 2 * cch
    if cch > 0x7FFF or end > len(body):  # sheet names and rIds are short
        raise _Bad("bad string length")
    return body[pos:end].decode("utf-16-le", errors="replace"), end


def parse_xlsb_bundle_sheets(data: bytes) -> list[tuple[str, str, str | None]] | None:
    """``(name, state, relationship id)`` per BrtBundleSh in ``xl/workbook.bin``.

    Record header: a 1-2 byte type and a 1-4 byte size, each 7 bits per byte (MS-XLSB
    2.1.4). LibreOffice's BIFF12 import is context-driven (BrtBundleSh only counts inside
    BrtBeginBook > BrtBeginBundleShs), so this reader only trusts the plain shape: the
    stream opens with BrtBeginBook, holds exactly ONE BrtBeginBundleShs..BrtEndBundleShs
    block, and no BrtBundleSh outside it. Anything else (a decoy block before the book,
    a second block) returns ``None``; so does a stream with no sheets.
    """
    pos = 0
    out: list[tuple[str, str, str | None]] = []
    first = True
    blocks = 0
    inside = False
    for _ in range(MAX_RECORDS):
        if pos >= len(data):
            break
        rtype, pos = _varint(data, pos, 2)
        size, pos = _varint(data, pos, 4)
        body = data[pos: pos + size]
        if len(body) != size:
            raise _Bad("truncated record")
        pos += size
        if first:
            if rtype != _BRT_BEGIN_BOOK:
                return None
            first = False
            continue
        if rtype == _BRT_BEGIN_BUNDLE_SHS:
            if inside or blocks:
                return None
            inside = True
            blocks += 1
            continue
        if rtype == _BRT_END_BUNDLE_SHS:
            if not inside:
                return None
            inside = False
            continue
        if rtype != _BRT_BUNDLE_SH:
            continue
        if not inside:
            return None
        if len(out) >= MAX_RECORD_SHEETS:
            return None
        if len(body) < 8:
            raise _Bad("short BrtBundleSh")
        hs, _tab_id = struct.unpack_from("<II", body, 0)
        if hs not in _STATES:
            raise _Bad("unknown BrtBundleSh state")
        rid, p = _wide_string(body, 8, nullable=True)
        name, _ = _wide_string(body, p, nullable=False)
        if not name:
            raise _Bad("BrtBundleSh without a name")
        # A NULL strRelID is valid for a VBA module sheet (MS-XLSB BrtBundleSh): report
        # it as such and let the name/order alignment decide.
        out.append((name, _STATES[hs], rid))
    else:
        return None  # record cap hit before the end: the rest could hold another block
    if inside:
        return None
    return out or None


# ─── alignment with the re-export inventory ──────────────────────────────────


def overlay_binary_records(path: Path, sheets: list[dict]) -> tuple[list[dict], str] | None:
    """See :func:`overlay_binary_records_status`; ``None`` when not overlaid."""
    got, _ = overlay_binary_records_status(path, sheets)
    return got


def overlay_binary_records_status(
    path: Path, sheets: list[dict], *, expect: str | None = None,
) -> tuple[tuple[list[dict], str] | None, str]:
    """Re-export inventory ``sheets`` with type/state taken from ``path``'s own records.

    Only when the two lists align exactly: the original's sheet records must carry the
    same names, in the same order, as the re-export (LibreOffice creates a tab for every
    BoundSheet8, hiding an xls VBA module sheet rather than dropping it, so module sheets
    stay in the alignment and are reported as ``vba_module``). Otherwise the result is
    ``None`` and the caller keeps the re-export inventory and its
    ``libreoffice_xlsx_reexport`` source. The status says why (``ok``,
    ``not_binary_workbook`` or one of :data:`RECORD_STATUSES`).
    """
    if not sheets:
        return None, "unaligned"
    got, status = read_binary_sheet_records_status(path)
    if got is None:
        return None, status
    source, records = got
    # The detected label says which importer LibreOffice used; records from the other
    # format (an xls-labelled zip, an xlsb-labelled CFB) are not what it rendered.
    if expect is not None and source != {"xls": XLS_SOURCE, "xlsb": XLSB_SOURCE}.get(expect):
        return None, "unreadable"
    if [r["name"] for r in records] != [s.get("name") for s in sheets]:
        return None, "unaligned"
    return ([{**s, "state": r["state"], "type": r["type"]}
             for s, r in zip(sheets, records)], source), "ok"
