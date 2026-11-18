"""XXE / entity-expansion-safe XML parsing for attacker-controlled parts.

``altchunk`` and ``sheet_prep`` parse ``[Content_Types].xml``,
``word/document.xml``, ``xl/workbook.xml`` etc. straight out of untrusted
uploads. Bare ``xml.etree.ElementTree.fromstring`` is only safe against
XXE / billion-laughs by virtue of the *platform* expat defaults — nothing
in the code asserts it, so a build on an older/vendored expat silently
reopens the hole.

Legitimate OPC/OOXML and flat-ODF parts never carry a DTD, so any document
that declares one is rejected outright. The rejection happens INSIDE an
expat parse (defusedxml semantics, without the dependency): expat calls the
DOCTYPE / entity / notation declaration handlers on the *decoded* character
stream, so the guard holds whatever the document's encoding. A byte-level
scan for ``<!DOCTYPE`` is blind to UTF-16 input (BOM or autodetected), where
those bytes are interleaved with NULs; expat refuses UTF-32 and declared
multi-byte encodings outright, and keeps ``<!DOCTYPE`` as ASCII bytes in every
single-byte encoding it accepts. The cheap ASCII byte pre-scan is kept as a
fast path for those common cases.
"""
from __future__ import annotations

import re
from typing import NoReturn
import xml.etree.ElementTree as ET
from xml.parsers import expat

# Fast path: reject an ASCII/UTF-8 DOCTYPE/ENTITY before building a parser. NOT
# the security boundary (it cannot see other encodings) — the parser handlers are.
_DTD_RE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

_DTD_REFUSED = "XML DOCTYPE/ENTITY declarations are not permitted"


class _DtdRefused(Exception):
    pass


def _refuse(*_args: object) -> NoReturn:
    raise _DtdRefused(_DTD_REFUSED)


def _reject_dtd(data: bytes) -> None:
    """Pre-parse with a bare expat parser whose DTD handlers abort.

    ``xml.etree``'s C-accelerated ``XMLParser`` exposes no hook on its expat
    instance, so the guard is a separate pass on the same expat library: same
    BOM/declaration-driven encoding detection as the real parse, so the two
    cannot disagree about where a DOCTYPE is. The DOCTYPE handler fires at the
    start of the declaration, before any internal-subset entity is defined, so
    nothing is ever expanded or fetched. The other handlers are
    belt-and-braces should a declaration ever reach expat without a DOCTYPE.
    """
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _refuse
    p.EntityDeclHandler = _refuse
    p.UnparsedEntityDeclHandler = _refuse
    p.NotationDeclHandler = _refuse
    p.ExternalEntityRefHandler = _refuse
    p.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        p.Parse(data, True)
    except _DtdRefused:
        raise ET.ParseError(_DTD_REFUSED) from None
    except (expat.ExpatError, ValueError, LookupError) as exc:
        # ValueError: expat's unknown-encoding hook refuses a declared multi-byte codec
        # ("multi-byte encodings are not supported"); callers only catch ParseError.
        raise ET.ParseError(str(exc)) from None


def safe_fromstring(data: bytes) -> ET.Element:
    """Parse XML, rejecting any DTD/entity declarations in any encoding.

    Raises ``ET.ParseError`` on a DOCTYPE/ENTITY/NOTATION declaration (or any
    malformed input) so callers can treat it the same as any other
    malformed-XML case (they already catch ``ET.ParseError`` and degrade
    gracefully).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ET.ParseError("safe_fromstring requires bytes")
    data = bytes(data)
    if _DTD_RE.search(data):
        raise ET.ParseError(_DTD_REFUSED)
    _reject_dtd(data)
    return ET.fromstring(data)
