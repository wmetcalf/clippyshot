"""Exception hierarchy for ClippyShot."""

from __future__ import annotations

import re


# Strip internal filesystem paths from public-facing error messages.
_INTERNAL_PATH_RE = re.compile(r"/(?:tmp|sandbox|var|home|opt|usr)/[^\s:;\"']+")


def sanitize_public_error(msg: str) -> str:
    """Remove internal filesystem paths from an error message."""
    return _INTERNAL_PATH_RE.sub("<path>", msg)


class ClippyShotError(Exception):
    """Base for all ClippyShot errors."""


class DetectionError(ClippyShotError):
    """Input was rejected by the detector."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class SandboxError(ClippyShotError):
    """Sandbox setup or execution failed."""


class SandboxTimeout(SandboxError):
    """Sandboxed process exceeded the wall-clock timeout."""


class SandboxUnavailable(SandboxError):
    """No usable sandbox backend on this host."""


class LibreOfficeError(ClippyShotError):
    """soffice failed or produced no output."""


class LibreOfficeEmptyOutputError(LibreOfficeError):
    """soffice exited cleanly but wrote no output PDF.

    This typically indicates the input was malformed or contained features
    LO refuses to render (a common defensive outcome for exploit fixtures
    targeting parsers LO no longer ships, e.g. the Microsoft Equation Editor
    CVE-2017-11882 family). It is distinct from a soffice crash or non-zero
    exit: the process completed normally, it just chose not to produce output.
    """


class RasterizeError(ClippyShotError):
    """Rasterizing the PDF failed."""


class ConversionError(ClippyShotError):
    """Top-level conversion failure carrying a wrapped cause."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
