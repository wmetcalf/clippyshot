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


def _cfb(n_entries: int, *, shift: int = 9, gap: int = 0, loop: bool = False,
         loaded: int | None = None) -> bytes:
    """A CFB whose root holds ``n_entries - 1`` one-byte ministreams in a balanced sibling
    tree (each non-empty stream feeds olefile's quadratic duplicate-stream list).

    ``gap`` free sectors precede the directory so its FAT entries land in FAT sectors past
    the header's 109 DIFAT slots (exercising the DIFAT chain). ``loop`` points the last
    directory sector back at the first. ``loaded`` < ``n_entries`` keeps only that many
    entries in the tree; the remaining slots are empty (a padded directory).
    """
    live = n_entries if loaded is None else loaded
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

    top = bal(1, live - 1)

    def ent(name: str, typ: int, lt: int, rt: int, c: int, start: int, size: int) -> bytes:
        nm = name.encode("utf-16-le") + b"\0\0"
        return (nm.ljust(64, b"\0") + struct.pack("<HBBIII", len(nm), typ, 1, lt, rt, c)
                + b"\0" * 36 + struct.pack("<IQ", start, size))

    d = bytearray(ent("Root Entry", 5, _NOSTREAM, _NOSTREAM, top, _EOC, 0))
    for i in range(1, live):
        d += ent(f"S{i:06d}", 2, left[i], right[i], _NOSTREAM, i - 1, 1)
    d = d.ljust(n_dir * S, b"\0")
    for k in range(live, n_dir * per_dir):
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
    """Records every OleFileIO construction (the plain class or the guard's subclass)."""
    calls: list[str] = []
    real = olefile.OleFileIO.__init__

    def spy(self, *a, **k):
        calls.append(str(a[0]) if a else "")
        return real(self, *a, **k)

    monkeypatch.setattr(olefile.OleFileIO, "__init__", spy)
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
    p = _write(tmp_path, "cap.xls", _cfb(40_000))
    reads: list[int] = []
    real_open = open

    class Counting:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

        def seek(self, *a):
            return self._fh.seek(*a)

        def read(self, n=-1):
            reads.append(n)
            return self._fh.read(n)

    monkeypatch.setattr(ole_guard, "open", lambda *a, **k: Counting(real_open(*a, **k)),
                        raising=False)
    with pytest.raises(ole_guard.OleGuardError, match="too_large"):
        ole_guard.check_cfb(p)
    # header + 16 directory sectors' FAT lookups (one FAT sector, cached); nowhere near the
    # 10,000 directory sectors of the file.
    assert len(reads) <= 4, reads
    monkeypatch.undo()
    monkeypatch.setattr(ole_guard, "MAX_OLE_DIR_ENTRIES", 64)
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
                                   _cfb(40_000, gap=109 * 128 + 10)))


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


# ─── the guard must read the FAT olefile reads (lens on #69) ─────────────────

def _decoy_difat_cfb(n_entries: int) -> bytes:
    """Header DIFAT = [FAT0, FREESECT, DECOY, ...], n_fat=110, one DIFAT sector.

    olefile stops reading the header DIFAT at the first FREESECT and takes FAT sector 1..
    from the DIFAT sector; read positionally, FAT sector 2 would be DECOY, which ends the
    directory after one sector while olefile's real FAT chains ``n_entries`` entries.
    """
    S, per_fat, per_dir = 512, 128, 4
    n_dir = -(-n_entries // per_dir)
    dir0 = 256
    last = dir0 + n_dir - 1
    n_real_fat = last // per_fat + 1
    assert n_real_fat - 1 <= 127
    fat_loc = {0: 0, **{k: 2 + k for k in range(1, n_real_fat)}}
    DIFAT0, DECOY = 1, 2
    fat = [_FREE] * (n_real_fat * per_fat)
    fat[0], fat[DIFAT0] = _FATSECT, _DIFSECT
    for k in range(1, n_real_fat):
        fat[fat_loc[k]] = _FATSECT
    for i in range(n_dir):
        fat[dir0 + i] = dir0 + i + 1 if i < n_dir - 1 else _EOC
    decoy = [_FREE] * per_fat
    decoy[dir0 - 2 * per_fat] = _EOC
    hdr = bytearray(_cfb(2)[:512])
    struct.pack_into("<IIIIIIII", hdr, 0x2C, 110, dir0, 0, 4096, _EOC, 0, DIFAT0, 1)
    struct.pack_into("<109I", hdr, 0x4C, 0, _FREE, DECOY, *([_FREE] * 106))
    difat0 = [fat_loc[k] for k in range(1, n_real_fat)]
    difat0 += [_FREE] * (per_fat - 1 - len(difat0)) + [_EOC]
    secs = {0: fat[:per_fat], DIFAT0: difat0, DECOY: decoy}
    for k in range(1, n_real_fat):
        secs[fat_loc[k]] = fat[k * per_fat:(k + 1) * per_fat]
    directory = _cfb(n_entries)[-(n_dir * S):]  # the same balanced directory, n_dir sectors
    body = bytearray()
    for i in range(last + 1):
        if dir0 <= i:
            body += directory[(i - dir0) * S:(i - dir0 + 1) * S]
        else:
            body += struct.pack(f"<{per_fat}I", *secs[i]) if i in secs else b"\0" * S
    return bytes(hdr) + bytes(body)


def test_decoy_difat_does_not_hide_the_real_directory(tmp_path, no_olefile_open):
    small = _write(tmp_path, "decoy-small.xls", _decoy_difat_cfb(400))
    with olefile.OleFileIO(str(small)) as ole:  # the shape olefile really parses
        assert len(ole.listdir()) == 399
    # (a file this small also trips the header-count bound: 110 FAT sectors for 0.6 MB)
    with pytest.raises(ole_guard.OleGuardError, match="unreadable"):
        ole_guard.check_cfb(small)
    no_olefile_open.clear()
    p = _write(tmp_path, "decoy.xls", _decoy_difat_cfb(40_000))
    t = time.process_time()
    with pytest.raises(ole_guard.OleGuardError, match="too_large"):
        ole_guard.check_cfb(p)
    assert _check_ole_content(p) == ["ole_limit_exceeded"]
    assert sr.read_binary_sheet_records_status(p) == (None, "too_large")
    assert time.process_time() - t < 1.0
    assert no_olefile_open == []


def test_difat_count_olefile_rejects_is_refused(tmp_path):
    b = bytearray(_cfb(10))
    struct.pack_into("<II", b, 0x44, 0, 1)  # first_difat=0, n_difat=1 with n_fat=1
    p = _write(tmp_path, "difat.xls", bytes(b))
    with pytest.raises(OSError):
        olefile.OleFileIO(str(p))
    with pytest.raises(ole_guard.OleGuardError, match="unreadable"):
        ole_guard.check_cfb(p)


def test_olefile_failure_after_the_guard_is_reported(tmp_path, monkeypatch):
    p = _write(tmp_path, "ok.xls", _cfb(10))

    def boom(self, *a, **k):
        self._we_opened_fp = False  # what olefile's __del__ looks at
        raise OSError("olefile could not parse it")

    monkeypatch.setattr(olefile.OleFileIO, "__init__", boom)
    assert _check_ole_content(p) == ["ole_unreadable"]


def test_opened_olefile_has_a_linear_duplicate_stream_check(tmp_path):
    """Second line of defence: even a directory the guard passes is parsed in linear time,
    and a stream referenced twice is still flagged."""
    p = _write(tmp_path, "edge.xls", _cfb(8192))  # plain olefile: ~0.3 s; linear: ~0.06 s
    t = time.process_time()
    with ole_guard.open_ole(p) as ole:
        assert len(ole.listdir()) == 8191
    assert time.process_time() - t < 0.2
    raw = bytearray(_cfb(4))
    # Entry 2's start minisector := entry 1's: the same stream referenced twice.
    dir_off = len(raw) - 512
    struct.pack_into("<I", raw, dir_off + 2 * 128 + 116, 0)
    dup = _write(tmp_path, "dup.xls", bytes(raw))
    with ole_guard.open_ole(dup) as ole:
        assert any("referenced twice" in str(m) for _k, m in ole.parsing_issues)


def test_difat_count_mismatch_alone_is_refused(tmp_path):
    """n_difat must equal what olefile derives from n_fat (olefile raises otherwise); here
    the DIFAT chain itself is empty, so only the count check can refuse."""
    b = bytearray(_cfb(10))
    struct.pack_into("<II", b, 0x44, _EOC, 1)  # first_difat=ENDOFCHAIN, n_difat=1, n_fat=1
    p = _write(tmp_path, "difat-eoc.xls", bytes(b))
    with pytest.raises(ole_guard.OleGuardError, match="DIFAT count"):
        ole_guard.check_cfb(p)


def test_spare_fat_count_without_difat_is_not_refused(tmp_path):
    """Without a DIFAT olefile ignores n_fat; a real .doc claiming spare FAT sectors (seen
    in the wild: n_fat 49 for a file that needs 6) parses cleanly and must not be refused."""
    raw = bytearray((_FIXTURES / "fixture.doc").read_bytes())
    struct.pack_into("<I", raw, 0x2C, 40)
    p = _write(tmp_path, "spare.doc", bytes(raw))
    with olefile.OleFileIO(str(p)) as ole:
        assert ole.num_difat_sectors == 0 and not ole.parsing_issues
    assert ole_guard.check_cfb(p) > 0
    assert "ole_unreadable" not in _check_ole_content(p)


def test_padded_directory_slots_are_refused_and_reported(tmp_path):
    """Slots are counted, not loaded entries: 3 live entries in a chain past the cap."""
    p = _write(tmp_path, "pad.xls", _cfb(ole_guard.MAX_OLE_DIR_ENTRIES + 4, loaded=3))
    with olefile.OleFileIO(str(p)) as ole:
        assert len(ole.listdir()) == 2
    assert _check_ole_content(p) == ["ole_limit_exceeded"]


def test_real_sized_large_directories_pass():
    """Real .doc files with 8k and 21k entries exist; the cap must admit them."""
    assert ole_guard.MAX_OLE_DIR_ENTRIES >= 22_000


def test_str_paths_work_everywhere(tmp_path):
    p = _FIXTURES / "fixture.xls"
    assert sr.read_binary_sheet_records_status(str(p))[1] == "ok"
    assert ole_guard.check_cfb(str(p)) > 0


def test_directory_chain_leaving_the_fat_stops_like_olefile(tmp_path):
    """olefile flags a directory chain pointing past the FAT and stops reading there
    (common in malformed dropper xls); the guard stops at the same place, not refuses."""
    raw = bytearray(_cfb(10))  # FAT sector 0 at file offset 512; directory = sectors 1..3
    struct.pack_into("<I", raw, 512 + 4 * 3, 5000)  # last directory sector -> out of range
    p = _write(tmp_path, "oor.xls", bytes(raw))
    with olefile.OleFileIO(str(p)) as ole:
        assert any("out of range" in str(m) for _k, m in ole.parsing_issues)
    assert ole_guard.check_cfb(p) == 12
    # olefile flagged the chain: reported (the parse is bounded and was not refused).
    assert _check_ole_content(p) == ["ole_unreadable"]


@pytest.mark.parametrize("target", [50, 5000])  # inside FAT sector 0 but past EOF; past both
def test_chain_into_the_fat_past_eof_stops_too(tmp_path, target):
    raw = bytearray(_cfb(10))
    struct.pack_into("<I", raw, 512 + 4 * 3, target)
    assert ole_guard.check_cfb(_write(tmp_path, "eof.xls", bytes(raw))) == 12


def test_chain_past_a_short_fat_stops_where_olefile_stops(tmp_path):
    """Header lists one FAT sector (128 entries) for a 152-sector file: olefile's FAT ends
    at sector 127 and its directory read stops there; the guard counts the same slots."""
    raw = bytearray(_cfb(600))
    struct.pack_into("<I", raw, 0x4C + 4, _FREE)  # drop the second FAT sector from DIFAT
    p = _write(tmp_path, "shortfat.xls", bytes(raw))
    with olefile.OleFileIO(str(p)) as ole:
        assert any("out of range" in str(m) for _k, m in ole.parsing_issues)
    assert ole_guard.check_cfb(p) == (128 - 2) * 4


def test_detector_reports_a_directory_olefile_stopped_reading(tmp_path):
    """olefile reads only part of the directory: the detector says unreadable (as
    sheet_records does), not a clean-looking empty check."""
    raw = bytearray(_cfb(600))
    struct.pack_into("<I", raw, 0x4C + 4, _FREE)
    p = _write(tmp_path, "partial.xls", bytes(raw))
    assert "ole_unreadable" in _check_ole_content(p)
    assert sr.read_binary_sheet_records_status(p) == (None, "unreadable")
