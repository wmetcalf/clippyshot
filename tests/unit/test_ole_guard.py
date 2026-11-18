"""Bounded CFB pre-check before every olefile open (olefile's constructor is quadratic in
directory entries: ~29 s CPU for a 10 MB xls with ~80k entries, paid in the detector and
again in sheet_records)."""
from __future__ import annotations

import struct
import time
from pathlib import Path

import olefile
import pytest

from clippyshot import ole_guard
from clippyshot.detector import _check_ole_content
from clippyshot.libreoffice import sheet_prep
from clippyshot.libreoffice import sheet_records as sr

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "safe"
_FREE, _EOC, _FATSECT, _DIFSECT, _NOSTREAM = (
    0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC, 0xFFFFFFFF)


def _cfb(n_entries: int, *, shift: int = 9, gap: int = 0, loop: bool = False) -> bytes:
    """A CFB whose root holds ``n_entries - 1`` one-byte ministreams in a balanced sibling
    tree (each non-empty stream feeds olefile's quadratic duplicate-stream list).

    ``gap`` free sectors precede the directory so its FAT entries land in FAT sectors past
    the header's 109 DIFAT slots (exercising the DIFAT chain). ``loop`` points the last
    directory sector back at the first.
    """
    S = 1 << shift
    per_fat, per_dir = S // 4, S // 128
    n_dir = -(-n_entries // per_dir)
    n_fat = n_difat = 0
    while True:  # FAT + DIFAT sectors must cover themselves too
        total = n_fat + n_difat + gap + n_dir
        need_fat = -(-total // per_fat)
        need_difat = 0 if need_fat <= 109 else -(-(need_fat - 109) // (per_fat - 1))
        if (need_fat, need_difat) == (n_fat, n_difat):
            break
        n_fat, n_difat = need_fat, need_difat
    fat_at = list(range(n_fat))
    difat_at = list(range(n_fat, n_fat + n_difat))
    dir0 = n_fat + n_difat + gap
    fat = [_FATSECT] * n_fat + [_DIFSECT] * n_difat + [_FREE] * gap
    for i in range(n_dir):
        fat.append(dir0 + i + 1 if i < n_dir - 1 else (dir0 if loop else _EOC))
    fat += [_FREE] * (n_fat * per_fat - len(fat))

    left: dict[int, int] = {}
    right: dict[int, int] = {}

    def bal(lo: int, hi: int) -> int:
        if lo > hi:
            return _NOSTREAM
        mid = (lo + hi) // 2
        left[mid], right[mid] = bal(lo, mid - 1), bal(mid + 1, hi)
        return mid

    top = bal(1, n_entries - 1)

    def ent(name: str, typ: int, lt: int, rt: int, c: int, start: int, size: int) -> bytes:
        nm = name.encode("utf-16-le") + b"\0\0"
        return (nm.ljust(64, b"\0") + struct.pack("<HBBIII", len(nm), typ, 1, lt, rt, c)
                + b"\0" * 36 + struct.pack("<IQ", start, size))

    d = bytearray(ent("Root Entry", 5, _NOSTREAM, _NOSTREAM, top, _EOC, 0))
    for i in range(1, n_entries):
        d += ent(f"S{i:06d}", 2, left[i], right[i], _NOSTREAM, i - 1, 1)
    d = d.ljust(n_dir * S, b"\0")
    for k in range(n_entries, n_dir * per_dir):
        struct.pack_into("<III", d, k * 128 + 68, _NOSTREAM, _NOSTREAM, _NOSTREAM)

    hdr = bytearray(512)
    hdr[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", hdr, 0x18, 0x3E, 3 if S == 512 else 4, 0xFFFE, shift, 6)
    struct.pack_into("<IIIIIIII", hdr, 0x2C, n_fat, dir0, 0, 4096, _EOC, 0,
                     difat_at[0] if difat_at else _EOC, n_difat)
    struct.pack_into("<109I", hdr, 0x4C, *(fat_at[:109] + [_FREE] * (109 - min(109, n_fat))))
    difat_secs = b""
    rest = fat_at[109:]
    for j in range(n_difat):
        chunk = rest[j * (per_fat - 1):(j + 1) * (per_fat - 1)]
        chunk += [_FREE] * (per_fat - 1 - len(chunk))
        nxt = difat_at[j + 1] if j + 1 < n_difat else _EOC
        difat_secs += struct.pack(f"<{per_fat}I", *chunk, nxt)
    return (bytes(hdr).ljust(S, b"\0") + struct.pack(f"<{len(fat)}I", *fat) + difat_secs
            + b"\0" * (gap * S) + bytes(d))


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.fixture
def no_olefile_open(monkeypatch):
    calls: list[str] = []
    real = olefile.OleFileIO

    def spy(*a, **k):
        calls.append(str(a[0]))
        return real(*a, **k)

    monkeypatch.setattr(olefile, "OleFileIO", spy)
    return calls


def test_builder_really_is_quadratic_in_olefile(tmp_path):
    """Sanity: the crafted file is the DoS shape (olefile parses it, slowly)."""
    p = _write(tmp_path, "q.xls", _cfb(4000))
    with olefile.OleFileIO(str(p)) as ole:
        assert len(ole.listdir()) == 3999


def test_many_entries_are_refused_before_olefile(tmp_path, no_olefile_open):
    # 40k entries: olefile alone spends several seconds of CPU on this (quadratic).
    p = _write(tmp_path, "many.xls", _cfb(40_000))
    t = time.process_time()
    with pytest.raises(ole_guard.OleGuardError) as exc:
        ole_guard.check_cfb(p)
    assert exc.value.reason == "too_large"
    assert _check_ole_content(p) == ["ole_limit_exceeded"]
    assert sr.read_binary_sheet_records_status(p) == (None, "too_large")
    assert sheet_prep._check_ole_workbook_size(p) is False
    assert time.process_time() - t < 1.0
    assert no_olefile_open == []


def test_walk_cost_is_bounded_by_the_cap_not_the_file(tmp_path, monkeypatch):
    """Reads stop at the cap: the chain past MAX_OLE_DIR_ENTRIES is never walked."""
    monkeypatch.setattr(ole_guard, "MAX_OLE_DIR_ENTRIES", 64)
    p = _write(tmp_path, "cap.xls", _cfb(4000))
    with pytest.raises(ole_guard.OleGuardError, match="too_large"):
        ole_guard.check_cfb(p)
    assert ole_guard.check_cfb(_write(tmp_path, "ok.xls", _cfb(64))) == 64


def test_entries_at_the_limit_pass_and_olefile_still_runs(tmp_path):
    p = _write(tmp_path, "edge.xls", _cfb(ole_guard.MAX_OLE_DIR_ENTRIES))
    assert ole_guard.check_cfb(p) == ole_guard.MAX_OLE_DIR_ENTRIES
    with pytest.raises(ole_guard.OleGuardError, match="too_large"):
        ole_guard.check_cfb(_write(tmp_path, "over.xls",
                                   _cfb(ole_guard.MAX_OLE_DIR_ENTRIES + 4)))


def test_directory_chain_loop_is_refused(tmp_path, no_olefile_open):
    p = _write(tmp_path, "loop.xls", _cfb(100, loop=True))
    with pytest.raises(ole_guard.OleGuardError) as exc:
        ole_guard.check_cfb(p)
    assert exc.value.reason == "unreadable"
    assert _check_ole_content(p) == ["ole_unreadable"]
    assert no_olefile_open == []


def test_directory_reached_through_the_difat_chain(tmp_path):
    # The directory's FAT entries sit in FAT sector >= 109: found via a DIFAT sector.
    p = _write(tmp_path, "difat.xls", _cfb(50, gap=109 * 128 + 10))
    assert ole_guard.check_cfb(p) == 52
    with olefile.OleFileIO(str(p)) as ole:
        assert len(ole.listdir()) == 49
    with pytest.raises(ole_guard.OleGuardError, match="too_large"):
        ole_guard.check_cfb(_write(tmp_path, "difat-many.xls",
                                   _cfb(20_000, gap=109 * 128 + 10)))


def test_4k_sector_cfb(tmp_path):
    p = _write(tmp_path, "v4.xls", _cfb(500, shift=12))
    assert ole_guard.check_cfb(p) == 512
    with olefile.OleFileIO(str(p)) as ole:
        assert len(ole.listdir()) == 499


@pytest.mark.parametrize("name", ["fixture.xls", "fixture.doc", "fixture.ppt"])
def test_real_office_fixtures_pass(name):
    assert 0 < ole_guard.check_cfb(_FIXTURES / name) <= ole_guard.MAX_OLE_DIR_ENTRIES


@pytest.mark.parametrize("mutate", [
    lambda b: b[:300],                                           # short header
    lambda b: b[:0x1E] + struct.pack("<H", 10) + b[0x20:],       # sector shift
    lambda b: b[:0x30] + struct.pack("<I", 0xFFFFFFF0) + b[0x34:],  # dir start invalid
    lambda b: b[:0x30] + struct.pack("<I", 10_000) + b[0x34:],   # dir start past EOF
])
def test_malformed_headers_are_unreadable(tmp_path, mutate):
    p = _write(tmp_path, "bad.xls", mutate(_cfb(10)))
    with pytest.raises(ole_guard.OleGuardError, match="unreadable"):
        ole_guard.check_cfb(p)
