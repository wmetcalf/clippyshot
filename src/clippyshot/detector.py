"""Magika-primary, extension-fallback file type detection."""

from __future__ import annotations

import zipfile
from pathlib import Path

from magika import Magika

from clippyshot.errors import DetectionError
from clippyshot.types import DetectedType


# Magika labels we accept directly. Maps Magika label → (canonical label, mime).
_SUPPORTED_MAGIKA: dict[str, tuple[str, str]] = {
    "docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    "xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    "doc": ("doc", "application/msword"),
    "xls": ("xls", "application/vnd.ms-excel"),
    "ppt": ("ppt", "application/vnd.ms-powerpoint"),
    "odt": ("odt", "application/vnd.oasis.opendocument.text"),
    "ods": ("ods", "application/vnd.oasis.opendocument.spreadsheet"),
    "odp": ("odp", "application/vnd.oasis.opendocument.presentation"),
    "odg": ("odg", "application/vnd.oasis.opendocument.graphics"),
    "rtf": ("rtf", "application/rtf"),
    "txt": ("txt", "text/plain"),
    "csv": ("csv", "text/csv"),
    "markdown": ("md", "text/markdown"),
    "xps": ("xps", "application/vnd.ms-xpsdocument"),
    "oxps": ("oxps", "application/oxps"),
    "xlsb": ("xlsb", "application/vnd.ms-excel.sheet.binary.macroEnabled.12"),
    # MIME HTML / "Single File Web Page" archives. Magika has no dedicated
    # label for these — they arrive tagged as docx/txt/unknown — so we set
    # the label ourselves via a content sniff in ``detect`` below and feed
    # the result back through this table for mime/canonical lookup.
    "mht": ("mht", "multipart/related"),
}


# Magika labels that represent generic/ambiguous container formats — fall back to
# extension in these cases (e.g. a minimal .docx that Magika only identifies as "zip").
# "unknown" is intentionally excluded: if Magika cannot identify the content at all,
# we reject rather than trust the extension blindly (spoofed-file defence).
# Note: "txtascii" was previously listed but Magika actually emits "txt" for ASCII
# text, so the entry was dead code (see L-6). Removed as part of H-3.
_GENERIC_LABELS = {"zip", "xml"}


# Maximum uncompressed:compressed ratio for a zip that we'll trust as OOXML.
# Measured: tests/fixtures/safe/tiny.docx ratio is well under 10:1. Real-world
# docx files produced by Word/LibreOffice sit in the 3–15:1 range. A flat
# ceiling of 100:1 gives a very wide safety margin while still catching
# bomb-class payloads (which typically exceed 1000:1).
_MAX_OOXML_COMPRESSION_RATIO = 100.0
_MAX_OOXML_ENTRIES = 5000

# Max <!ENTITY declarations in an XML we'll trust as (F)ODF. Real flat-ODF
# documents do not declare entities; any file with more than this is almost
# certainly a billion-laughs bomb.
_MAX_XML_ENTITY_COUNT = 64


def _looks_like_ooxml(
    path: Path,
    *,
    max_compression_ratio: float = _MAX_OOXML_COMPRESSION_RATIO,
    max_entries: int = _MAX_OOXML_ENTRIES,
) -> bool:
    """Sanity-check that a file labeled 'zip' looks like a real OOXML document.

    Rejects:
    - Files that are not valid zip archives
    - Zip bombs (compression ratio > max_compression_ratio, or > max_entries)
    - Zips that don't contain [Content_Types].xml (the OOXML marker)
    """
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.namelist()
            if len(entries) > max_entries:
                return False
            if "[Content_Types].xml" not in entries:
                return False
            total_compressed = 0
            total_uncompressed = 0
            for info in zf.infolist():
                total_compressed += info.compress_size
                total_uncompressed += info.file_size
            if total_compressed == 0:
                # Empty or store-only zero-size entries: only accept if the
                # uncompressed total is also zero (i.e. an empty zip).
                return total_uncompressed == 0
            ratio = total_uncompressed / total_compressed
            return ratio <= max_compression_ratio
    except (zipfile.BadZipFile, OSError):
        return False


def _looks_like_safe_xml(
    path: Path, *, max_entity_count: int = _MAX_XML_ENTITY_COUNT
) -> bool:
    """Sanity-check that a file labeled 'xml' is not a billion-laughs bomb.

    Streaming scan: count <!ENTITY occurrences; abort as soon as the limit is
    exceeded. This is a cheap heuristic sufficient to block the classic entity
    expansion attack without pulling in a real XML parser (expat already has a
    CVE track record on this).
    """
    try:
        ent_count = 0
        with open(path, "rb") as f:
            overlap = b""
            for chunk in iter(lambda: f.read(8192), b""):
                buf = overlap + chunk
                ent_count += buf.count(b"<!ENTITY")
                if ent_count > max_entity_count:
                    return False
                overlap = chunk[-7:] if len(chunk) >= 7 else chunk
        return True
    except OSError:
        return False


# Extension allowlist used as fallback. Maps lowercase ext → (canonical label, mime).
_EXT_ALLOWLIST: dict[str, tuple[str, str]] = {
    "docx": _SUPPORTED_MAGIKA["docx"],
    "xlsx": _SUPPORTED_MAGIKA["xlsx"],
    "pptx": _SUPPORTED_MAGIKA["pptx"],
    "doc": _SUPPORTED_MAGIKA["doc"],
    "xls": _SUPPORTED_MAGIKA["xls"],
    "ppt": _SUPPORTED_MAGIKA["ppt"],
    "odt": _SUPPORTED_MAGIKA["odt"],
    "ods": _SUPPORTED_MAGIKA["ods"],
    "odp": _SUPPORTED_MAGIKA["odp"],
    "odg": _SUPPORTED_MAGIKA["odg"],
    "rtf": _SUPPORTED_MAGIKA["rtf"],
    "txt": _SUPPORTED_MAGIKA["txt"],
    "csv": _SUPPORTED_MAGIKA["csv"],
    "md": _SUPPORTED_MAGIKA["markdown"],
    "markdown": _SUPPORTED_MAGIKA["markdown"],
    "xps": _SUPPORTED_MAGIKA["xps"],
    "oxps": _SUPPORTED_MAGIKA["oxps"],
    # Word-family OOXML variants — structurally identical to docx, Magika
    # labels them all as "docx".
    "docm": _SUPPORTED_MAGIKA["docx"],  # macro-enabled document
    "dotx": _SUPPORTED_MAGIKA["docx"],  # template
    "dotm": _SUPPORTED_MAGIKA["docx"],  # macro-enabled template
    # Word-family legacy template
    "dot": _SUPPORTED_MAGIKA["doc"],
    # Excel-family OOXML variants
    "xlsm": _SUPPORTED_MAGIKA["xlsx"],  # macro-enabled workbook
    "xltx": _SUPPORTED_MAGIKA["xlsx"],  # template
    "xltm": _SUPPORTED_MAGIKA["xlsx"],  # macro-enabled template
    "xlam": _SUPPORTED_MAGIKA["xlsx"],  # add-in
    "xlsb": _SUPPORTED_MAGIKA["xlsb"],  # binary workbook
    # Excel-family legacy template / add-in
    "xlt": _SUPPORTED_MAGIKA["xls"],
    "xla": _SUPPORTED_MAGIKA["xls"],
    # PowerPoint-family OOXML variants
    "pptm": _SUPPORTED_MAGIKA["pptx"],  # macro-enabled presentation
    "ppsx": _SUPPORTED_MAGIKA["pptx"],  # show
    "ppsm": _SUPPORTED_MAGIKA["pptx"],  # macro-enabled show
    "potx": _SUPPORTED_MAGIKA["pptx"],  # template
    "potm": _SUPPORTED_MAGIKA["pptx"],  # macro-enabled template
    "ppam": _SUPPORTED_MAGIKA["pptx"],  # add-in
    # PowerPoint-family legacy variants
    "pps": _SUPPORTED_MAGIKA["ppt"],
    "pot": _SUPPORTED_MAGIKA["ppt"],
    "ppa": _SUPPORTED_MAGIKA["ppt"],
    # ODF templates
    "ott": _SUPPORTED_MAGIKA["odt"],
    "ots": _SUPPORTED_MAGIKA["ods"],
    "otp": _SUPPORTED_MAGIKA["odp"],
    "otg": _SUPPORTED_MAGIKA["odg"],
    # Flat ODF (single-file XML variants) — Magika labels these as "xml"
    # which then falls through to the extension allowlist via the generic
    # label path. They open in LibreOffice via the same import filters.
    "fodt": _SUPPORTED_MAGIKA["odt"],
    "fods": _SUPPORTED_MAGIKA["ods"],
    "fodp": _SUPPORTED_MAGIKA["odp"],
    "fodg": _SUPPORTED_MAGIKA["odg"],
    # MIME HTML archives. LO opens these via its Writer MHTML importer
    # when the file has a ``.mht`` / ``.mhtml`` extension — ``rewrite_active``
    # in the runner takes care of the rename when uploads arrive with a
    # spoofed extension (e.g. .docx hiding a MHT payload).
    "mht": ("mht", "multipart/related"),
    "mhtml": ("mht", "multipart/related"),
    # HTML — Writer handles natively
    "html": ("txt", "text/plain"),
    "htm": ("txt", "text/plain"),
}


# Extensions that declare "this file may contain macros". The hardened
# LibreOffice profile (MacroSecurityLevel=3, DisableMacrosExecution=true)
# prevents execution, but downstream consumers may want to know that the
# input format CAN carry macros so they can apply audit policy.
MACRO_ENABLED_EXTENSIONS: frozenset[str] = frozenset(
    {
        "docm",
        "dotm",
        "xlsm",
        "xltm",
        "xlam",
        "xlsb",
        "xla",  # workbook/template/add-in/binary variants can carry macros
        "pptm",
        "ppsm",
        "potm",
        "ppam",
        "ppa",
    }
)


# MIME types that indicate text-family content renderable by Writer.
# Used as a last-resort fallback: if Magika identifies a specific type we
# don't have a dedicated converter for (e.g., powershell, pem, python,
# shell, json, yaml, html), but the MIME says it's text, we render it as
# plain text via Writer rather than rejecting it.
_TEXT_MIME_EXACT = frozenset(
    {
        "application/json",
        "application/x-powershell",
        "application/x-pem-file",
        "application/javascript",
        "application/xml",
        "application/x-yaml",
        "application/toml",
        "application/x-httpd-php",
        "application/x-sh",
        "application/x-csh",
    }
)


# Labels where libmagic should be consulted to correct the Office app family.
_OFFICE_LABELS = frozenset(
    {
        # OOXML
        "docx",
        "xlsx",
        "pptx",
        # Legacy OLE — Magika often confuses these because they're all OLE
        # compound documents with similar structure
        "doc",
        "xls",
        "ppt",
        # RTF — sometimes misidentified
        "rtf",
        # Text types that might be misidentified
        "txt",
        "csv",
    }
)

# libmagic MIME → our canonical label mapping
_LIBMAGIC_MIME_TO_LABEL: dict[str, str] = {}
_LIBMAGIC_MIME_PATTERNS: list[tuple[str, str]] = [
    # OOXML (detected by content types inside the zip)
    ("presentationml", "pptx"),
    ("wordprocessingml", "docx"),
    ("spreadsheetml", "xlsx"),
    # Legacy OLE (detected by OLE stream names)
    ("vnd.ms-powerpoint", "ppt"),
    ("msword", "doc"),
    ("vnd.ms-excel", "xls"),
    # RTF
    ("text/rtf", "rtf"),
    ("application/rtf", "rtf"),
    # CSV
    ("text/csv", "csv"),
    # Catch-all OLE types (some files report as generic OLE)
    ("x-ole-storage", None),  # don't correct — too ambiguous
    ("CDFV2", None),
]


def _looks_like_mht(path: Path) -> bool:
    """Sniff the first 8 KB for MIME/Multipart markers (olevba-style).

    Word and Excel's "Save as Web Page, Filtered" produces MHTML files
    that may start with whitespace or extra headers before the canonical
    ``MIME-Version: 1.0`` line. Checking for the three tokens anywhere in
    the first chunk, plus a proximity constraint between ``mime`` and
    ``version``, catches both variants without false-positive-ing on
    unrelated files that happen to contain all three words spread out.

    Based on oletools/olevba.py's MHT heuristic.
    """
    try:
        with path.open("rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if not head:
        return False
    low = head.lower()
    if b"mime" not in low or b"version" not in low or b"multipart" not in low:
        return False
    try:
        return abs(low.index(b"version") - low.index(b"mime")) < 20
    except ValueError:
        return False


def _correct_office_label_via_libmagic(path: Path, magika_label: str) -> str:
    """Correct Magika's Office label using libmagic's deterministic MIME.

    libmagic reads format-specific signatures directly:
    - OOXML: parses [Content_Types].xml inside the zip
    - OLE: reads stream names (PowerPoint Document, Workbook, WordDocument)

    This reliably distinguishes Word/Excel/PowerPoint even when Magika's
    ML classifier guesses wrong. Falls back to zip directory inspection
    for OOXML files if libmagic isn't available.
    """
    try:
        import magic as _magic

        m = _magic.Magic(mime=True)
        detected_mime = m.from_file(str(path))
        for pattern, label in _LIBMAGIC_MIME_PATTERNS:
            if pattern in detected_mime and label is not None:
                return label
    except Exception:
        pass

    # Fall back to zip directory inspection (OOXML only — OLE can't be
    # corrected without libmagic or olefile)
    if magika_label in ("docx", "xlsx", "pptx"):
        import zipfile

        try:
            with zipfile.ZipFile(str(path)) as zf:
                names = zf.namelist()
                has_ppt = any(n.startswith("ppt/") for n in names)
                has_word = any(n.startswith("word/") for n in names)
                has_xl = any(n.startswith("xl/") for n in names)
                if has_ppt and not has_word and not has_xl:
                    return "pptx"
                if has_word and not has_ppt and not has_xl:
                    return "docx"
                if has_xl and not has_ppt and not has_word:
                    return "xlsx"
        except (zipfile.BadZipFile, OSError, KeyError):
            pass
    return magika_label


def _get_libmagic_mime(path: Path) -> str:
    """Get the MIME type from libmagic. Returns empty string on failure."""
    try:
        import magic as _magic

        return _magic.Magic(mime=True).from_file(str(path))
    except Exception:
        return ""


def _check_ole_content(path: Path) -> list[str]:
    """Check an OLE compound document for missing content streams.

    Returns a list of warnings. An OLE file with VBA macros but no
    document content stream (PowerPoint Document, WordDocument, Workbook)
    is almost certainly a macro-only payload — there's nothing to render.
    """
    warnings = []
    try:
        import olefile

        if not olefile.isOleFile(str(path)):
            return warnings
        ole = olefile.OleFileIO(str(path))
        dirs = ["/".join(d) for d in ole.listdir()]
        has_vba = any("VBA" in d or "Macros" in d for d in dirs)
        has_ppt = any(s in dirs for s in ["PowerPoint Document", "PP97_DUALSTORAGE"])
        has_doc = any(s in dirs for s in ["WordDocument", "1Table", "0Table"])
        has_xls = any(s in dirs for s in ["Workbook", "Book"])
        has_encrypted = "EncryptedPackage" in dirs
        has_content = has_ppt or has_doc or has_xls or has_encrypted
        if has_vba and not has_content:
            warnings.append("ole_macro_only_payload")
        if has_encrypted:
            warnings.append("ole_encrypted")
        ole.close()
    except Exception:
        pass
    return warnings


def _check_ooxml_content(path: Path) -> list[str]:
    """Check an OOXML zip for empty/missing core document XML.

    A valid OOXML file must have a non-empty core document:
    - ppt/presentation.xml for PPTX
    - word/document.xml for DOCX
    - xl/workbook.xml for XLSX

    Files with a 0-byte core document or suspicious non-standard entries
    (like woking.bin, payload.exe) are likely delivery mechanisms, not
    real documents.
    """
    warnings = []
    try:
        with zipfile.ZipFile(str(path)) as zf:
            names = zf.namelist()
            # Check for core document XML
            core_files = {
                "ppt/presentation.xml": "pptx",
                "word/document.xml": "docx",
                "xl/workbook.xml": "xlsx",
            }
            for core, fmt in core_files.items():
                if core in names:
                    info = zf.getinfo(core)
                    if info.file_size == 0:
                        warnings.append("ooxml_empty_core_document")
                        break
            # Check for suspicious non-standard entries
            standard_prefixes = (
                "ppt/",
                "word/",
                "xl/",
                "_rels/",
                "docProps/",
                "[Content_Types]",
            )
            suspicious = [
                n
                for n in names
                if not any(n.startswith(p) for p in standard_prefixes)
                and n.endswith((".bin", ".exe", ".dll", ".dat", ".vbs", ".js"))
            ]
            if suspicious:
                warnings.append("ooxml_suspicious_entries")
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return warnings


def _detect_by_magic_bytes(path: Path) -> str | None:
    """Detect format by file magic bytes. Only returns a label when the
    bytes are unambiguous — otherwise returns None."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if len(head) < 4:
        return None
    # RTF: {\rt is definitive — nothing else starts with this
    if head[:4] == b"{\\rt":
        return "rtf"
    return None


def _is_text_mime(mime: str) -> bool:
    """Return True if the MIME type indicates text-family content."""
    if mime.startswith("text/"):
        return True
    return mime in _TEXT_MIME_EXACT


class Detector:
    """File type detection using Magika as primary and extension as fallback."""

    def __init__(self) -> None:
        self._magika = Magika()

    def detect(self, path: Path, *, max_input_bytes: int | None = None) -> DetectedType:
        path = Path(path)
        size = path.stat().st_size
        if max_input_bytes is not None and size > max_input_bytes:
            raise DetectionError(
                "input_too_large",
                f"{size} bytes exceeds limit {max_input_bytes}",
            )

        ext = path.suffix.lstrip(".").lower()
        result = self._magika.identify_path(path)
        raw_magika_label: str = str(result.output.label)
        magika_label = raw_magika_label
        magika_score: float = float(result.score)
        magika_mime: str = str(getattr(result.output, "mime_type", "") or "")
        libmagic_mime: str = _get_libmagic_mime(path)

        # Office format correction via libmagic: Magika's ML classifier
        # often confuses the Office app family. libmagic is deterministic
        # and reads format-specific signatures directly.
        if magika_label in _OFFICE_LABELS and size > 0:
            corrected = _correct_office_label_via_libmagic(path, magika_label)
            if corrected != magika_label:
                magika_label = corrected

        # MHT override: magika routinely tags Word/Excel-saved MHTMLs as
        # docx with high confidence because the outer OOXML metadata in
        # the MHT wrapper fools the ML signature. libmagic misses them
        # too. A cheap first-8-KB sniff for MIME-Version + multipart
        # (olevba's approach) catches them before the OOXML code paths
        # try to unwrap a non-existent zip.
        if size > 0 and _looks_like_mht(path):
            magika_label = "mht"

        # Structural content checks: detect files that are valid containers
        # but have no actual document content to render.
        _structural_warnings: list[str] = []
        if magika_label in ("doc", "xls", "ppt") and size > 0:
            _structural_warnings.extend(_check_ole_content(path))
        if magika_label in ("docx", "xlsx", "pptx") and size > 0:
            _structural_warnings.extend(_check_ooxml_content(path))

        # Magic-byte fallback: if Magika says unknown/wrong and libmagic
        # didn't help, check the first few bytes for definitive signatures
        # (e.g. {\rt for RTF).
        if magika_label not in _SUPPORTED_MAGIKA:
            byte_label = _detect_by_magic_bytes(path)
            if byte_label and byte_label in _SUPPORTED_MAGIKA:
                magika_label = byte_label

        # Common fields for all return paths
        _extra = dict(
            magika_label=raw_magika_label,
            magika_mime=magika_mime,
            libmagic_mime=libmagic_mime,
        )

        # Happy path: Magika identified a type we explicitly support.
        if magika_label in _SUPPORTED_MAGIKA:
            canonical, mime = _SUPPORTED_MAGIKA[magika_label]
            # Zip-bomb check on OOXML even when Magika confidently
            # classified the file. Magika uses structural signals to label
            # xlsx/docx/pptx — a crafted zip bomb can still get that label
            # while exploding to gigabytes when LibreOffice opens it.
            if magika_label in ("docx", "xlsx", "pptx") and not _looks_like_ooxml(path):
                raise DetectionError(
                    "ooxml_structural_check_failed",
                    f"magika={magika_label} failed OOXML bomb/entry/ratio check",
                )
            return DetectedType(
                label=canonical,
                mime=mime,
                extension_hint=ext,
                confidence=magika_score,
                source="magika",
                agreed_with_extension=(
                    ext == canonical
                    or (ext in _EXT_ALLOWLIST and _EXT_ALLOWLIST[ext][0] == canonical)
                ),
                warnings=list(_structural_warnings),
                **_extra,
            )

        # Magika returned a known-generic label (zip, xml): the file is a valid
        # container but Magika couldn't be more specific. Fall back to the
        # extension allowlist only after a structural sanity check (H-3):
        # - zip → must look like a real OOXML document (compression ratio
        #   under 100:1, < 5000 entries, contains [Content_Types].xml).
        # - xml → must not declare more than 64 entities (billion-laughs).
        if magika_label in _GENERIC_LABELS:
            if ext in _EXT_ALLOWLIST:
                if magika_label == "zip" and not _looks_like_ooxml(path):
                    raise DetectionError(
                        "magika_unknown_extension_mismatch",
                        f"magika=zip ext={ext} (failed OOXML structural check)",
                    )
                if magika_label == "xml" and not _looks_like_safe_xml(path):
                    raise DetectionError(
                        "magika_unknown_extension_mismatch",
                        f"magika=xml ext={ext} (failed XML safety check)",
                    )
                canonical, mime = _EXT_ALLOWLIST[ext]
                return DetectedType(
                    label=canonical,
                    mime=mime,
                    extension_hint=ext,
                    confidence=0.0,
                    source="extension",
                    agreed_with_extension=True,
                    warnings=[f"magika_labeled_as_{magika_label}"],
                    **_extra,
                )
            raise DetectionError(
                "magika_unknown_extension_mismatch",
                f"magika={magika_label} ext={ext or '<none>'}",
            )

        # Magika returned "unknown" — it genuinely can't classify the content.
        # This happens for files that are too small, encrypted, obfuscated, or
        # use unusual encoding (common in malware samples).
        #
        # If the extension is in our allowlist, we accept it with a warning and
        # let LibreOffice try. LO has its own import filters and will either
        # render what it can or fail gracefully. The warning lets downstream
        # consumers know we're trusting the extension, not the content.
        #
        # Note: the REAL spoofed-file defense is the "unsupported_type" path
        # below — when Magika CONFIDENTLY identifies a file as something we
        # don't support (e.g., PDF bytes saved as .docx → Magika says "pdf" →
        # rejected). "unknown" means Magika has no opinion, which is different
        # from Magika saying "this is definitely the wrong format."
        if magika_label == "unknown":
            if ext in _EXT_ALLOWLIST:
                canonical, mime = _EXT_ALLOWLIST[ext]
                return DetectedType(
                    label=canonical,
                    mime=mime,
                    extension_hint=ext,
                    confidence=0.0,
                    source="extension",
                    agreed_with_extension=True,
                    warnings=["magika_unrecognized_content"],
                    **_extra,
                )
            raise DetectionError(
                "magika_unknown_extension_mismatch",
                f"magika={magika_label} ext={ext or '<none>'}",
            )

        # Magika identified a type we don't have a specific handler for.
        # But if the MIME indicates it's text-family, Writer can render it as
        # plain text. This catches scripts (powershell, shell, python, perl),
        # config files (json, yaml, toml), certificates (pem), and any other
        # text-based format Magika identifies specifically but we don't have
        # a dedicated converter for.
        #
        # This is a LAST RESORT — only reached if no specific handler, no
        # generic-container fallback, and no unknown+extension fallback matched.
        if _is_text_mime(magika_mime):
            return DetectedType(
                label="txt",
                mime="text/plain",
                extension_hint=ext,
                confidence=magika_score,
                source="magika",
                agreed_with_extension=(ext == "txt"),
                warnings=[f"rendered_as_text_from_{magika_label}"],
                **_extra,
            )

        # Genuinely unsupported binary format (pdf, exe, elf, pe, image, etc.).
        raise DetectionError(
            "unsupported_type",
            f"magika={magika_label}",
        )
