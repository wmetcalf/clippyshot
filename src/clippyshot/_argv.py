"""Shared argv-safety helpers for external tool invocations."""
from __future__ import annotations


def assert_positional(path: object) -> None:
    """Reject a positional path that an option parser could mistake for a flag.

    tesseract, ZXingReader and pdftoppm all take the input file as a bare
    positional, and none support a portable ``--`` end-of-options terminator
    (tesseract errors on ``--``; ZXingReader prints usage and never reads the
    file). The only way a path is parsed as an option is if its string starts
    with ``-`` — our paths are always absolute ``/sandbox/...`` or host paths,
    so this is a cheap invariant rather than a real constraint, but it removes
    the standing "safe only by convention" assumption flagged in audit #11.
    """
    s = str(path)
    if s.startswith("-"):
        raise ValueError(f"refusing option-like positional path: {s!r}")
