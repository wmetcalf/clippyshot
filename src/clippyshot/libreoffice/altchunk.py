"""OOXML altChunk inspector / extractor.

Word's ``<w:altChunk>`` feature (ECMA-376 Part 1, §17.17) lets a docx
embed content in an alternative format — HTML, MHT, Word 97-2003, or
another OOXML fragment — to be inlined when Word opens the file.
Attackers abuse this by wrapping a malicious MHT inside an otherwise
empty docx: AV tools scan the docx, see nothing interesting, and miss
the payload entirely. Legitimate uses exist but are vanishingly rare.

This module parses ``[Content_Types].xml`` to find altChunk-eligible
Overrides, then pulls the raw part bytes out of the zip. Callers can
decide how to handle each altChunk (extract, render separately, flag
as a warning). Handles only docx-family inputs — other OOXML (xlsx,
pptx) use different embedded-object mechanisms.
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path


_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_ALTCHUNK_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
)

# Content types that Word treats as altChunk payloads. The spec lists
# more (``text/plain``, ``application/xhtml+xml``, etc.) but in
# malware corpora the vast majority are ``message/rfc822`` (MHT).
_ALTCHUNK_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "message/rfc822",
        "text/html",
        "application/xhtml+xml",
        "application/vnd.ms-word.document.macroEnabled.main+xml",
        "application/msword",
    }
)


@dataclass(frozen=True)
class AltChunk:
    part_name: str  # e.g. "/word/afchunk.mht"
    content_type: str  # e.g. "message/rfc822"
    size: int
    data: bytes


def inspect_altchunks(docx_path: Path) -> list[AltChunk]:
    """Return every altChunk-eligible part in a docx, in declared order.

    Follows actual ``w:altChunk`` relationships from ``word/document.xml``
    in document order rather than trusting any altChunk-like part merely
    declared in ``[Content_Types].xml``.

    Empty list on non-zip inputs, missing core parts, or parse failures —
    altChunk inspection is a defensive best-effort.
    """
    if not zipfile.is_zipfile(docx_path):
        return []
    try:
        with zipfile.ZipFile(docx_path) as zf:
            try:
                types_xml = zf.read("[Content_Types].xml")
            except KeyError:
                return []
            try:
                root = ET.fromstring(types_xml)
            except ET.ParseError:
                return []
            content_types = {
                override.attrib.get("PartName", ""): override.attrib.get(
                    "ContentType", ""
                )
                for override in root.findall(f"{_TYPES_NS}Override")
            }
            try:
                document_xml = ET.fromstring(zf.read("word/document.xml"))
                rels_xml = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
            except (KeyError, ET.ParseError, zipfile.BadZipFile, OSError):
                return []

            rel_targets: dict[str, str] = {}
            for rel in rels_xml.findall(f"{_REL_NS}Relationship"):
                rel_id = rel.attrib.get("Id", "")
                if not rel_id:
                    continue
                if rel.attrib.get("Type") != _ALTCHUNK_REL_TYPE:
                    continue
                if rel.attrib.get("TargetMode", "").lower() == "external":
                    continue
                target = rel.attrib.get("Target", "")
                if not target:
                    continue
                part_name = posixpath.normpath(posixpath.join("/word", target))
                if not part_name.startswith("/"):
                    part_name = "/" + part_name
                rel_targets[rel_id] = part_name

            found: list[AltChunk] = []
            for chunk in document_xml.findall(f".//{_WORD_NS}altChunk"):
                rel_id = chunk.attrib.get(f"{_DOC_REL_NS}id", "")
                if not rel_id:
                    continue
                part_name = rel_targets.get(rel_id)
                if not part_name:
                    continue
                content_type = content_types.get(part_name, "")
                if content_type not in _ALTCHUNK_CONTENT_TYPES:
                    continue
                try:
                    data = zf.read(part_name.lstrip("/"))
                except (KeyError, OSError, zipfile.BadZipFile):
                    continue
                found.append(
                    AltChunk(
                        part_name=part_name,
                        content_type=content_type,
                        size=len(data),
                        data=data,
                    )
                )
            return found
    except (zipfile.BadZipFile, OSError):
        return []
