"""Cheap, bounded CFB (OLE2) pre-check run before every ``olefile.OleFileIO`` open.

olefile's constructor is quadratic in the number of directory entries (every non-empty
stream's first sector is checked against a *list* of those already seen), and it sizes its
DIFAT/FAT/directory work from header counts and a sector chain an attacker controls. A
~10 MB xls with ~80k directory entries costs ~29 s of CPU per open. This module reads the
512-byte header and walks the directory sector chain through the FAT itself -- a bounded
number of small reads, independent of the entry count -- and refuses the file before olefile
ever sees it when:

* the header is short or its sector shifts are not CFB's (9 or 12, mini 6),
* a FAT / MiniFAT / DIFAT sector count is absurd for the file's size,
* the directory chain is longer than :data:`MAX_OLE_DIR_ENTRIES` entries allow, loops,
  or leaves the file.

Refusals raise :class:`OleGuardError` with ``reason`` ``"too_large"`` (a limit) or
``"unreadable"`` (a malformed header or chain); callers report it, never drop it silently.
"""
from __future__ import annotations

import struct
from pathlib import Path

# Directory entries a CFB may carry before we refuse to hand it to olefile. Real Office
# documents carry tens to a few hundred (each embedded object adds a storage and a handful
# of streams); olefile's quadratic duplicate-stream check costs ~0.3 s CPU at this bound
# versus ~29 s at 80k.
MAX_OLE_DIR_ENTRIES = 8192

_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_MAXREGSECT = 0xFFFFFFFA
_HEADER_DIFAT = 109

REASONS = frozenset({"too_large", "unreadable"})


class OleGuardError(Exception):
    """The CFB pre-check refused the file; ``reason`` is ``too_large`` or ``unreadable``."""

    def __init__(self, reason: str, detail: str) -> None:
        assert reason in REASONS
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def check_cfb(path: Path, *, max_entries: int | None = None) -> int:
    """Validate ``path``'s CFB header and directory chain; return the directory entry slots.

    Raises :class:`OleGuardError` instead of letting olefile do unbounded work. Cost is
    O(directory sectors walked + FAT sectors read), both capped by ``max_entries``
    (default :data:`MAX_OLE_DIR_ENTRIES`).
    """
    limit = MAX_OLE_DIR_ENTRIES if max_entries is None else max_entries
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        h = fh.read(512)
        if len(h) < 512 or h[:8] != _MAGIC:
            raise OleGuardError("unreadable", "not a CFB header")
        shift, mini_shift = struct.unpack_from("<HH", h, 0x1E)
        if shift not in (9, 12) or mini_shift != 6:
            raise OleGuardError("unreadable", f"sector shift {shift}/{mini_shift}")
        sector = 1 << shift
        (n_fat, first_dir, _txn, _cutoff, _first_mini, n_mini, first_difat,
         n_difat) = struct.unpack_from("<IIIIIIII", h, 0x2C)
        # Sector i starts at (i + 1) * sector: the header occupies sector -1.
        n_sectors = -(-max(0, size - sector) // sector)
        per_fat = sector // 4
        # Spare FAT sectors are legal, so allow generous slack over what the file needs;
        # the bound only has to stop counts that are absurd for the file's size (olefile
        # loops over and copies them in its constructor).
        needed_fat = n_sectors // per_fat + 1
        if (n_fat > 2 * needed_fat + 16 or n_mini > n_sectors
                or n_difat > n_fat // (per_fat - 1) + 2):
            raise OleGuardError("unreadable", "header sector counts exceed the file")

        def read_sector(idx: int) -> bytes:
            if idx >= n_sectors:
                raise OleGuardError("unreadable", f"sector {idx} beyond the file")
            fh.seek((idx + 1) * sector)
            data = fh.read(sector)
            if len(data) != sector:
                raise OleGuardError("unreadable", f"sector {idx} truncated")
            return data

        difat_cache: list[int] = list(struct.unpack_from(f"<{_HEADER_DIFAT}I", h, 0x4C))
        difat_next = first_difat
        difat_hops = 0
        fat_cache: dict[int, tuple[int, ...]] = {}

        def fat_sector_location(k: int) -> int:
            nonlocal difat_next, difat_hops
            if k >= n_fat:
                raise OleGuardError("unreadable", "FAT index beyond the FAT")
            while k >= len(difat_cache):
                if difat_next > _MAXREGSECT or difat_hops >= n_difat:
                    raise OleGuardError("unreadable", "DIFAT chain ends early")
                difat_hops += 1
                entries = struct.unpack(f"<{per_fat}I", read_sector(difat_next))
                difat_cache.extend(entries[:-1])
                difat_next = entries[-1]
            return difat_cache[k]

        def fat_entry(sect: int) -> int:
            k, off = divmod(sect, per_fat)
            if k not in fat_cache:
                loc = fat_sector_location(k)
                if loc > _MAXREGSECT:
                    raise OleGuardError("unreadable", "FAT sector unallocated")
                fat_cache[k] = struct.unpack(f"<{per_fat}I", read_sector(loc))
            return fat_cache[k][off]

        per_dir = sector // 128
        max_dir_sectors = -(-limit // per_dir)
        sect = first_dir
        walked = 0
        seen: set[int] = set()
        while sect != _ENDOFCHAIN:
            if sect > _MAXREGSECT or sect >= n_sectors:
                raise OleGuardError("unreadable", f"directory sector {sect:#x} invalid")
            if sect in seen:
                raise OleGuardError("unreadable", "directory chain loops")
            if walked >= max_dir_sectors:
                raise OleGuardError(
                    "too_large",
                    f"directory exceeds {limit} entries ({walked * per_dir}+ slots)")
            seen.add(sect)
            walked += 1
            sect = fat_entry(sect)
        if walked == 0:
            raise OleGuardError("unreadable", "empty directory chain")
    return walked * per_dir


def open_ole(path: Path):
    """``olefile.OleFileIO`` for ``path`` after :func:`check_cfb` passed (raises otherwise)."""
    import olefile

    check_cfb(path)
    return olefile.OleFileIO(str(path))
