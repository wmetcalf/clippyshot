"""Unit tests for the WarmOCR persistent helper — lifecycle + pipe protocol,
exercised with an injected fake Popen (no real tesseract/tesserocr needed)."""
import base64
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


def _page(tmp_path: Path, n: int = 1) -> Path:
    """A real file on disk. The client reads the image and sends its BYTES, so a
    path that does not exist is no longer a usable stand-in -- and its CONTENT is
    what a fake helper can correlate on, which is a stronger pairing than a
    filename."""
    png = tmp_path / f"page-{n:03d}.png"
    png.write_bytes(b"PAGE-%d-" % n + b"\x89PNG\r\n\x1a\n" + bytes([n]) * 32)
    return png


def _server(responses):
    proc = FakeProc(responses)
    return WarmOCR(popen=lambda *a, **k: proc), proc


def test_start_ready_then_ocr(tmp_path):
    srv, _ = _server([{"text": "hello world", "char_count": 11, "duration_ms": 5}])
    srv.start()
    assert srv.is_ready() is True
    r = srv.ocr(_page(tmp_path), lang="eng+Latin", psm=3, timeout_s=30)
    assert isinstance(r, OCRResult)
    assert r.text == "hello world" and r.char_count == 11 and r.duration_ms == 5


def test_error_response_raises_ocrerror(tmp_path):
    srv, _ = _server([{"error": "Leptonica: bad png"}])
    srv.start()
    with pytest.raises(OCRError):
        srv.ocr(_page(tmp_path), lang="eng", psm=3, timeout_s=30)


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
        # Correlate on the PAYLOAD, not a filename: the request carries the image
        # itself now, so this pairs response to request on the bytes that were sent.
        n = int(base64.b64decode(self._last["png_b64"]).split(b"-")[1])
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


def test_concurrent_ocr_pairs_request_to_response(tmp_path):
    # Finding 1: the converter OCRs pages from a thread pool sharing one WarmOCR.
    # The lock must serialise the single pipe so each page gets ITS OWN response.
    proc = EchoProc()
    srv = WarmOCR(popen=lambda *a, **k: proc)
    srv.start()
    results: dict[int, int] = {}
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            r = srv.ocr(_page(tmp_path, n), lang="eng", psm=3, timeout_s=5)
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


def test_hung_helper_times_out_and_is_killed(tmp_path):
    # Finding 2: a hung page must NOT block forever; ocr() times out, kills the
    # helper, and raises OCRError so the converter cold-falls-back.
    proc = HangProc()
    srv = WarmOCR(popen=lambda *a, **k: proc)
    srv.start()
    t0 = time.monotonic()
    with pytest.raises(OCRError):
        srv.ocr(_page(tmp_path), lang="eng", psm=3, timeout_s=0.2)
    assert time.monotonic() - t0 < 5  # did not hang
    assert srv.is_ready() is False  # helper was killed


def test_a_queued_call_is_priced_after_the_lock_not_before(tmp_path):
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
    srv.ocr(_page(tmp_path), lang="eng", psm=3, timeout_s=60, deadline=deadline)
    holder.join(2.0)

    sent = json.loads(proc.stdin.getvalue().strip().splitlines()[-1])
    assert sent["timeout_s"] < 1.0 - wait_s + 0.1, (
        f"the queued wait was not deducted: helper was given {sent['timeout_s']}s "
        f"of a 1.0s budget after waiting {wait_s}s"
    )
    assert sent["timeout_s"] > 0


def test_a_call_whose_budget_expired_while_queued_is_refused(tmp_path):
    """Past the deadline there is nothing left to spend; the caller cold-falls-back."""
    srv, _ = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
    srv.start()
    with pytest.raises(OCRError, match="budget exhausted"):
        srv.ocr(
            _page(tmp_path), lang="eng", psm=3,
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


class TestThePageTravelsAsBytes:
    """The helper may be SANDBOXED, and then it has no access to the job filesystem at
    all -- so the image cannot be passed as a path (issue #35). Sending bytes is also
    what makes a persistent sandboxed helper possible: its mounts are fixed at spawn,
    before any page exists.
    """

    def test_the_request_carries_the_image_not_its_path(self, tmp_path):
        srv, proc = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
        srv.start()
        png = _page(tmp_path, 7)
        srv.ocr(png, lang="eng", psm=3, timeout_s=30)

        sent = json.loads(proc.stdin.getvalue().strip().splitlines()[-1])
        assert "png" not in sent, "the helper was handed a path it may not be able to read"
        assert base64.b64decode(sent["png_b64"]) == png.read_bytes()
        assert str(png) not in proc.stdin.getvalue(), (
            "the page's host path reached the helper anyway"
        )

    def test_an_unreadable_page_fails_before_anything_is_sent(self, tmp_path):
        srv, proc = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
        srv.start()
        with pytest.raises(OCRError, match="cannot read"):
            srv.ocr(tmp_path / "does-not-exist.png", lang="eng", psm=3, timeout_s=30)
        assert "png_b64" not in proc.stdin.getvalue()

    def test_an_oversized_page_is_refused_rather_than_streamed(self, tmp_path, monkeypatch):
        """The bound on one request's memory: the caller cold-falls-back to the CLI,
        which reads the file directly and needs no such budget."""
        import clippyshot.ocr_warm as mod

        monkeypatch.setattr(mod, "MAX_PNG_BYTES", 64)
        srv, proc = _server([{"text": "ok", "char_count": 2, "duration_ms": 1}])
        srv.start()
        big = tmp_path / "big.png"
        big.write_bytes(b"x" * 65)
        with pytest.raises(OCRError, match="over the"):
            srv.ocr(big, lang="eng", psm=3, timeout_s=30)
        assert "png_b64" not in proc.stdin.getvalue(), "an oversized page was sent anyway"


class TestSandboxedSpawn:
    """`sandboxed_popen` is what lets the warm tier run under nsjail/bwrap at all --
    before it, the engine declined and fell back to the per-page CLI (issue #35)."""

    class _FakeSandbox:
        name = "fake"

        def __init__(self):
            self.request = None
            self.kwargs = None

        def spawn(self, request, **kwargs):
            self.request = request
            self.kwargs = kwargs
            return "POPEN"

    def _spawn(self, **kw):
        from clippyshot.ocr_warm import sandboxed_popen

        sb = self._FakeSandbox()
        popen = sandboxed_popen(sb, **kw)
        handle = popen(["/x/python", "-m", "clippyshot.ocr_warm", "serve"], stdin=-1, text=True)
        return sb, handle

    def test_no_job_data_is_mounted(self, tmp_path):
        """The point of the whole change: the helper sees its interpreter and nothing
        else. A mount of the job's output would put the pages back inside."""
        sb, _ = self._spawn(prefix=str(tmp_path))
        assert sb.request.rw_mounts == []
        mounted = {str(m.host_path) for m in sb.request.ro_mounts}
        assert str(tmp_path) in mounted
        assert all(not str(m.host_path).startswith("/job") for m in sb.request.ro_mounts)

    def test_the_soffice_apparmor_profile_is_not_attached(self, tmp_path):
        """It is written for soffice; this is a different binary. Namespaces, uid,
        seccomp and the rlimits still apply."""
        sb, _ = self._spawn(prefix=str(tmp_path))
        assert sb.request.attach_apparmor is False

    def test_popen_kwargs_reach_the_backend(self, tmp_path):
        """WarmOCR owns the pipes; if these were swallowed there would be no transport."""
        sb, handle = self._spawn(prefix=str(tmp_path))
        assert handle == "POPEN"
        assert sb.kwargs["stdin"] == -1 and sb.kwargs["text"] is True

    def test_a_system_prefix_is_not_remounted(self):
        """/usr is already mounted by both backends; mounting it again is at best noise
        and at worst a duplicate-mount error."""
        sb, _ = self._spawn(prefix="/usr")
        assert all(str(m.host_path) != "/usr" for m in sb.request.ro_mounts)


class TestTheHelperHonoursConfiguredLimits:
    """A deployment that caps memory is capping what an untrusted image parser may
    allocate, and the cold scanner path honours it. The long-lived warm parser is the
    one process where that cap matters most (codex)."""

    class _FakeSandbox:
        name = "fake"
        request = None

        def spawn(self, request, **kwargs):
            type(self).request = request
            return "POPEN"

    def _limits(self, monkeypatch, tmp_path, **env):
        from clippyshot.ocr_warm import sandboxed_popen

        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sb = self._FakeSandbox()
        sandboxed_popen(sb, prefix=str(tmp_path))(["/x/python", "serve"])
        return sb.request.limits

    def test_a_configured_memory_cap_reaches_the_helper(self, monkeypatch, tmp_path):
        limits = self._limits(monkeypatch, tmp_path, CLIPPYSHOT_MEM=str(512 * 1024 * 1024))
        assert limits.memory_bytes == 512 * 1024 * 1024, (
            "the warm parser was given the default cap, not the configured one"
        )

    def test_the_default_still_applies_when_nothing_is_configured(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLIPPYSHOT_MEM", raising=False)
        from clippyshot.limits import Limits

        assert self._limits(monkeypatch, tmp_path).memory_bytes == Limits().memory_bytes
