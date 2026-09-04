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


class RaisingProc:
    """terminate()/wait()/kill() all raise — an already-reaped process."""

    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(json.dumps({"ready": True}) + "\n")

    def poll(self):
        return None  # appears running

    def terminate(self):
        raise ProcessLookupError("already reaped")

    def wait(self, timeout=None):
        raise ChildProcessError()

    def kill(self):
        raise ProcessLookupError()


def test_stop_is_robust_when_terminate_raises():
    # Teardown (atexit / timeout-kill races) must never raise even if the
    # process was already reaped.
    proc = RaisingProc()
    srv = WarmOCR(popen=lambda *a, **k: proc)
    srv.start()
    srv.stop()  # must NOT propagate ProcessLookupError/ChildProcessError
    assert srv._proc is None


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


def test_a_queued_call_is_priced_after_the_lock_not_before():
    """The REAL lock, held by another thread — a fake helper cannot show this.

    Every caller serialises here. A duration computed before the wait is still
    the full duration when the lock is acquired, so one job budget stretches
    across every queued page. An absolute deadline does not decay.
    """
    srv, proc = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
    srv.start()

    held = threading.Event()
    release = threading.Event()

    def hold_the_lock():
        with srv._lock:
            held.set()
            release.wait(2.0)

    holder = threading.Thread(target=hold_the_lock, daemon=True)
    holder.start()
    assert held.wait(2.0), "the lock holder never started"

    wait_s = 0.25
    deadline = time.monotonic() + 1.0
    threading.Timer(wait_s, release.set).start()
    srv.ocr(Path("/x/page-001.png"), lang="eng", psm=3, timeout_s=60, deadline=deadline)
    holder.join(2.0)

    sent = json.loads(proc.stdin.getvalue().strip().splitlines()[-1])
    assert sent["timeout_s"] < 1.0 - wait_s + 0.1, (
        f"the queued wait was not deducted: helper was given {sent['timeout_s']}s "
        f"of a 1.0s budget after waiting {wait_s}s"
    )
    assert sent["timeout_s"] > 0


def test_a_call_whose_budget_expired_while_queued_is_refused():
    """Past the deadline there is nothing left to spend; the caller cold-falls-back."""
    srv, _ = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
    srv.start()
    with pytest.raises(OCRError, match="budget exhausted"):
        srv.ocr(
            Path("/x/page-001.png"), lang="eng", psm=3,
            timeout_s=60, deadline=time.monotonic() - 0.01,
        )


def test_the_helper_caches_a_bounded_number_of_language_apis():
    """`lang` and `psm` are job-controlled, and the image ships every language.

    An unbounded cache let a long-lived warm slot accumulate one loaded model
    per distinct pair until it exhausted the slot's memory.
    """
    import clippyshot.ocr_warm as ow

    ended = []

    class FakeAPI:
        def __init__(self, **kw):
            self.kw = kw

        def SetVariable(self, *a):
            pass

        def End(self):
            ended.append(self.kw.get("lang"))

    api_for = ow._build_api_cache(lambda **kw: FakeAPI(**kw), tessdata=None)

    for lang in ("eng", "deu", "fra", "spa", "ita", "por"):
        api_for(lang, 3)
    assert ended == ["eng", "deu"], f"the two oldest must be End()ed, got {ended}"

    # A repeat use is a cache HIT and refreshes recency: it must not be evicted
    # next, and must not construct a second API for the same pair.
    before = api_for("fra", 3)
    assert api_for("fra", 3) is before, "a cached pair must be reused, not rebuilt"
    api_for("nld", 3)
    assert "fra" not in ended, "the recently used entry must survive eviction"
