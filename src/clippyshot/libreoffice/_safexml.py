"""XXE / entity-expansion-safe XML parsing for attacker-controlled parts.

``altchunk`` and ``sheet_prep`` parse ``[Content_Types].xml``,
``word/document.xml``, ``xl/workbook.xml`` etc. straight out of untrusted
uploads. Bare ``xml.etree.ElementTree.fromstring`` is only safe against
XXE / billion-laughs by virtue of the *platform* expat defaults — nothing
in the code asserts it, so a build on an older/vendored expat silently
reopens the hole.

We deliberately do not pull in a full hardened-parser dependency
(``defusedxml``); the detector already established the house style of a
cheap byte pre-scan (``_looks_like_safe_xml``). Legitimate OPC/OOXML and
flat-ODF parts never carry a ``<!DOCTYPE`` or ``<!ENTITY`` declaration, so
rejecting any document that does blocks external-entity (XXE) and
internal entity-expansion (billion-laughs) attacks outright, independent
of the expat version underneath.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Match a DOCTYPE or ENTITY declaration anywhere in the prolog/body. Both are
# absent from well-formed office XML parts; their presence means either an
# XXE attempt (DOCTYPE ... SYSTEM) or an entity-expansion bomb.
_DTD_RE = re.compile(rb"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def safe_fromstring(data: bytes) -> ET.Element:
    """Parse XML, rejecting any DTD/entity declarations.

    Raises ``ET.ParseError`` on a DOCTYPE/ENTITY declaration so callers can
    treat it the same as any other malformed-XML case (they already catch
    ``ET.ParseError`` and degrade gracefully).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ET.ParseError("safe_fromstring requires bytes")
    if _DTD_RE.search(data):
        raise ET.ParseError("XML DOCTYPE/ENTITY declarations are not permitted")
    return ET.fromstring(data)
