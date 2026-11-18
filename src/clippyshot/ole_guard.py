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

import functools
import struct
from pathlib import Path

# Directory entries a CFB may carry before we refuse to hand it to olefile. Real Office
# documents carry tens to a few hundred (each embedded object adds a storage and a handful
# of streams); olefile's quadratic duplicate-stream check costs ~0.3 s CPU at this bound
# versus ~29 s at 80k.
MAX_OLE_DIR_ENTRIES = 8192

_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_MAXREGSECT = 0xFFFFFFFA
_HEADER_DIFAT = 109

REASONS = frozenset({"too_large", "unreadable"})


def _until_terminator(entries) -> list[int]:
    """olefile.loadfat_sect: FAT sector numbers up to the first ENDOFCHAIN / FREESECT."""
    out: list[int] = []
    for e in entries:
        if e in (_ENDOFCHAIN, _FREESECT):
            break
        out.append(e)
    return out


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

        # The FAT-sector list exactly as olefile's loadfat builds it: the header's 109 slots
        # and then each DIFAT sector's entries, each list cut at its first ENDOFCHAIN /
        # FREESECT and the remainder appended straight after. Indexing the slots by position
        # instead would follow a different FAT than olefile whenever a terminator sits
        # before a later entry -- and validate a decoy directory chain.
        fat_secs = _until_terminator(struct.unpack_from(f"<{_HEADER_DIFAT}I", h, 0x4C))
        if n_difat:
            # olefile derives the DIFAT sector count from n_fat and raises unless the
            # header's count matches; n_difat is already bounded by the file size above.
            nb_difat = (n_fat - _HEADER_DIFAT + per_fat - 2) // (per_fat - 1)
            if n_difat != nb_difat:
                raise OleGuardError("unreadable", "DIFAT count disagrees with the FAT count")
            isect = first_difat
            for _ in range(nb_difat):
                entries = struct.unpack(f"<{per_fat}I", read_sector(isect))
                fat_secs += _until_terminator(entries[:-1])
                isect = entries[-1]
            if isect not in (_ENDOFCHAIN, _FREESECT):
                raise OleGuardError("unreadable", "DIFAT chain does not end")
        fat_cache: dict[int, tuple[int, ...]] = {}

        def fat_entry(sect: int) -> int:
            k, off = divmod(sect, per_fat)
            if k not in fat_cache:
                if k >= len(fat_secs):
                    raise OleGuardError("unreadable", "sector beyond the FAT")
                fat_cache[k] = struct.unpack(f"<{per_fat}I", read_sector(fat_secs[k]))
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


@functools.cache
def _bounded_olefile_class():
    """``OleFileIO`` with a set-based duplicate-stream check.

    olefile keeps the first sectors of the streams it has seen in a *list*, so its
    constructor is O(n^2) in directory entries. Same check, same defect, O(1) per entry:
    a second line of defence should the pre-check and olefile ever read a directory
    differently.
    """
    import olefile

    class _OleFileIO(olefile.OleFileIO):
        def _check_duplicate_stream(self, first_sect, minifat=False):
            if not minifat and first_sect in (olefile.DIFSECT, olefile.FATSECT,
                                              olefile.ENDOFCHAIN, olefile.FREESECT):
                return
            attr = "_cs_seen_minifat" if minifat else "_cs_seen_fat"
            seen = self.__dict__.setdefault(attr, set())
            if first_sect in seen:
                self._raise_defect(olefile.DEFECT_INCORRECT, "Stream referenced twice")
            else:
                seen.add(first_sect)

    return _OleFileIO


def open_ole(path: Path):
    """An olefile ``OleFileIO`` for ``path`` after :func:`check_cfb` passed (raises
    otherwise), with olefile's quadratic duplicate-stream check made linear."""
    check_cfb(path)
    return _bounded_olefile_class()(str(path))
