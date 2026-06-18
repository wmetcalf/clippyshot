"""Unit tests for the WarmOCR persistent helper — lifecycle + pipe protocol,
exercised with an injected fake Popen (no real tesseract/tesserocr needed)."""
import io
import json
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
