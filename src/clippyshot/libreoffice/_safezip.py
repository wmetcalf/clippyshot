"""Bounded zip extraction — defense against decompression bombs.

The detector's only bomb guard is an *aggregate* compression-ratio check
(``_looks_like_ooxml``), which an attacker dilutes with incompressible
STORED padding: keep the whole-archive ratio under 100:1 while one inner
entry expands to gigabytes. Every in-process extractor that materializes
decompressed bytes into RAM (``altchunk``, ``sheet_prep``, the MHT bytes
that feed ``mht_unpack``) must therefore defend *itself* with per-entry
and cumulative caps rather than trusting an upstream invariant that lives
in a different module and a different code path.

Caps are read once from the environment so they ride the same
``CLIPPYSHOT_*`` configuration channel as everything else. Defaults are
generous enough for any legitimate office document but bounded well below
what would OOM a worker.
"""
from __future__ import annotations

import os
import zipfile


class ExtractionLimitExceeded(Exception):
    """A zip entry (or a cumulative extraction pass) exceeded a safety cap."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return default
    return val if val > 0 else default


# Per-entry decompressed-byte ceiling (one zip member read into RAM).
MAX_ENTRY_BYTES = _env_int("CLIPPYSHOT_MAX_EXTRACT_ENTRY_BYTES", 256 * 1024 * 1024)
# Cumulative decompressed-byte ceiling across a single extraction pass.
MAX_TOTAL_BYTES = _env_int("CLIPPYSHOT_MAX_EXTRACT_TOTAL_BYTES", 512 * 1024 * 1024)
# Maximum number of entries materialized in one extraction pass.
MAX_ENTRIES = _env_int("CLIPPYSHOT_MAX_EXTRACT_ENTRIES", 10000)

_CHUNK = 1 << 20  # 1 MiB streaming read


def bounded_read(
    zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_ENTRY_BYTES
) -> bytes:
    """Read one zip member, refusing to materialize more than ``max_bytes``.

    Streams the entry rather than calling ``zf.read(name)`` whole, so an
    entry whose advertised ``file_size`` lies (or whose true expansion is a
    bomb) cannot blow past the cap before we notice. Raises
    ``ExtractionLimitExceeded`` instead of letting RSS climb unbounded.
    """
    info = zf.getinfo(name)
    # Cheap pre-check against the declared size; the streaming loop below is
    # the authoritative guard since a crafted header can understate the size.
    if info.file_size > max_bytes:
        raise ExtractionLimitExceeded(
            f"zip entry {name!r} declares {info.file_size} bytes > cap {max_bytes}"
        )
    out = bytearray()
    with zf.open(name) as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            out += chunk
            if len(out) > max_bytes:
                raise ExtractionLimitExceeded(
                    f"zip entry {name!r} exceeded cap {max_bytes} during read"
                )
    return bytes(out)


class ExtractionBudget:
    """Tracks cumulative decompressed bytes + entry count across one pass.

    Use a single budget for the whole extraction of one archive so that N
    medium entries can't sum to a bomb even if each is individually under
    the per-entry cap.
    """

    def __init__(
        self, max_total: int = MAX_TOTAL_BYTES, max_entries: int = MAX_ENTRIES
    ) -> None:
        self.max_total = max_total
        self.max_entries = max_entries
        self.total = 0
        self.entries = 0

    def read(
        self, zf: zipfile.ZipFile, name: str, max_entry: int = MAX_ENTRY_BYTES
    ) -> bytes:
        self.entries += 1
        if self.entries > self.max_entries:
            raise ExtractionLimitExceeded(
                f"zip exceeded entry cap {self.max_entries}"
            )
        remaining = self.max_total - self.total
        if remaining <= 0:
            raise ExtractionLimitExceeded(
                f"zip exceeded cumulative extraction cap {self.max_total}"
            )
        data = bounded_read(zf, name, max_bytes=min(max_entry, remaining))
        self.total += len(data)
        return data
