"""Unit tests for the WarmOCR persistent helper — lifecycle + pipe protocol,
exercised with an injected fake Popen (no real tesseract/tesserocr needed)."""
import io
import json
import threading
import time
from pathlib import Path

import pytest

from clippyshot.ocr import OCRError, OCRResult
from clippyshot.ocr_warm import WarmOCR


class FakeProc:
    """A stand-in subprocess: stdout is pre-loaded with a ``ready`` line plus
    one response line per queued response; stdin is a sink."""

    def __init__(self, responses):
        lines = [{"ready": True}] + list(responses)
        self.stdout = io.StringIO("".join(json.dumps(r) + "\n" for r in lines))
        self.stdin = io.StringIO()
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def kill(self):
        self._rc = -9


def _server(responses):
    proc = FakeProc(responses)
    return WarmOCR(popen=lambda *a, **k: proc), proc


def test_start_ready_then_ocr():
    srv, _ = _server([{"text": "hello world", "char_count": 11, "duration_ms": 5}])
    srv.start()
    assert srv.is_ready() is True
    r = srv.ocr(Path("/x/page-001.png"), lang="eng+Latin", psm=3, timeout_s=30)
    assert isinstance(r, OCRResult)
    assert r.text == "hello world" and r.char_count == 11 and r.duration_ms == 5


def test_error_response_raises_ocrerror():
    srv, _ = _server([{"error": "Leptonica: bad png"}])
    srv.start()
    with pytest.raises(OCRError):
        srv.ocr(Path("/x/p.png"), lang="eng", psm=3, timeout_s=30)


def test_dead_helper_not_ready():
    srv, proc = _server([])
    srv.start()
    proc._rc = 1
    assert srv.is_ready() is False


def test_ocr_when_not_ready_raises():
    srv, proc = _server([])
    srv.start()
    proc._rc = 1  # helper died
    with pytest.raises(OCRError):
        srv.ocr(Path("/x/p.png"), lang="eng", psm=3, timeout_s=30)


class EchoProc:
    """Returns a response derived from the most-recently-written request — so a
    correct result requires write+read to be serialised (the WarmOCR lock).
    Interleaving (no lock) clobbers _last and mis-pairs page↔text."""

    def __init__(self):
        self._first = True
        self._last = None
        self._rc = None
        self.stdin = self
        self.stdout = self

    def write(self, s):
        s = s.strip()
        if s:
            req = json.loads(s)
            if "cmd" not in req:
                self._last = req

    def flush(self):
        pass

    def readline(self):
        if self._first:
            self._first = False
            return json.dumps({"ready": True}) + "\n"
        n = int(self._last["png"].rsplit("-", 1)[1].split(".")[0])
        return json.dumps({"text": f"page{n}", "char_count": n, "duration_ms": 1}) + "\n"

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def kill(self):
        self._rc = -9


def test_concurrent_ocr_pairs_request_to_response():
    # Finding 1: the converter OCRs pages from a thread pool sharing one WarmOCR.
    # The lock must serialise the single pipe so each page gets ITS OWN response.
    proc = EchoProc()
    srv = WarmOCR(popen=lambda *a, **k: proc)
    srv.start()
    results: dict[int, int] = {}
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            r = srv.ocr(Path(f"/x/page-{n:03d}.png"), lang="eng", psm=3, timeout_s=5)
            results[n] = r.char_count
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 13)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert results == {n: n for n in range(1, 13)}  # each thread got its own page


class HangProc:
    """Helper whose OCR read blocks until terminate()/kill() — a hung tesseract."""

    def __init__(self):
        self._first = True
        self._ev = threading.Event()
        self._rc = None
        self.stdin = io.StringIO()
        self.stdout = self

    def readline(self):
        if self._first:
            self._first = False
            return json.dumps({"ready": True}) + "\n"
        self._ev.wait(10)  # block until killed (capped so the test can't hang)
        return ""

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0
        self._ev.set()

    def wait(self, timeout=None):
        self._rc = 0
        self._ev.set()
        return 0

    def kill(self):
        self._rc = -9
        self._ev.set()


def test_hung_helper_times_out_and_is_killed():
    # Finding 2: a hung page must NOT block forever; ocr() times out, kills the
    # helper, and raises OCRError so the converter cold-falls-back.
    proc = HangProc()
    srv = WarmOCR(popen=lambda *a, **k: proc)
    srv.start()
    t0 = time.monotonic()
    with pytest.raises(OCRError):
        srv.ocr(Path("/x/page-001.png"), lang="eng", psm=3, timeout_s=0.2)
    assert time.monotonic() - t0 < 5  # did not hang
    assert srv.is_ready() is False  # helper was killed
