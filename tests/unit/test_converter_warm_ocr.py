"""The converter routes OCR through a warm helper when present, and falls
closed to the cold CLI path (`_ocr_fn`) on any helper error."""
from clippyshot.converter import _process_page_scanners
from clippyshot.ocr import OCRError, OCRResult


class Helper:
    def __init__(self, result=None, raises=False):
        self.result, self.raises, self.calls = result, raises, 0

    def is_ready(self):
        return True

    def ocr(self, png, *, lang, psm, timeout_s, deadline=None):
        self.calls += 1
        if self.raises:
            raise OCRError("boom")
        return self.result


def _scan(tmp_path, helper, ocr_fn):
    rec = {"file": "page-001.png", "index": 1, "width": 100, "height": 100}
    (tmp_path / "page-001.png").write_bytes(b"x")
    return _process_page_scanners(
        tmp_path, rec,
        is_blank=False, qr_enabled=False, qr_formats="", qr_timeout_s=5,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=3,
        ocr_time_left=lambda: 60.0, has_images=True, ocr_all=True,
        _ocr_fn=ocr_fn, ocr_helper=helper,
    )


def test_helper_used_when_ready(tmp_path):
    h = Helper(result=OCRResult("warm text", 9, 7))
    called = {"cli": 0}

    def cli(*a, **k):
        called["cli"] += 1
        return OCRResult("cli", 3, 1)

    _, _, ocr_obj, _ = _scan(tmp_path, h, cli)
    assert ocr_obj["text"] == "warm text"
    assert h.calls == 1 and called["cli"] == 0


def test_falls_back_to_cli_on_helper_error(tmp_path):
    h = Helper(raises=True)

    def cli(*a, **k):
        return OCRResult("cli text", 8, 2)

    _, _, ocr_obj, warnings = _scan(tmp_path, h, cli)
    assert ocr_obj["text"] == "cli text"
    assert h.calls == 1
    assert any(w["code"] == "ocr_warm_fallback" for w in warnings)


def test_warm_fallback_recomputes_ocr_budget(tmp_path):
    # On warm->CLI fallback the CLI must use the RECOMPUTED remaining budget
    # (ocr_time_left), not the stale pre-warm value — so a slow/hung warm attempt
    # can't make the job spend ~2x the per-page budget.
    h = Helper(raises=True)
    seen = {}

    def cli(scan_png, *, lang, psm, timeout_s, argv_runner=None):
        seen["timeout_s"] = timeout_s
        return OCRResult("cli", 3, 1)

    calls = {"n": 0}

    def time_left():
        calls["n"] += 1
        return 50.0 if calls["n"] == 1 else 12.0  # warm consumed budget → 12 left

    rec = {"file": "page-001.png", "index": 1, "width": 100, "height": 100}
    (tmp_path / "page-001.png").write_bytes(b"x")
    _process_page_scanners(
        tmp_path, rec, is_blank=False, qr_enabled=False, qr_formats="", qr_timeout_s=5,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=3, ocr_time_left=time_left,
        has_images=True, ocr_all=True, _ocr_fn=cli, ocr_helper=h,
    )
    assert seen["timeout_s"] == 12  # recomputed remaining, not the pre-warm 50


def test_no_helper_uses_cli(tmp_path):
    called = {"cli": 0}

    def cli(*a, **k):
        called["cli"] += 1
        return OCRResult("cli only", 8, 1)

    rec = {"file": "page-001.png", "index": 1, "width": 100, "height": 100}
    (tmp_path / "page-001.png").write_bytes(b"x")
    _, _, ocr_obj, _ = _process_page_scanners(
        tmp_path, rec,
        is_blank=False, qr_enabled=False, qr_formats="", qr_timeout_s=5,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=3,
        ocr_time_left=lambda: 60.0, has_images=True, ocr_all=True,
        _ocr_fn=cli, ocr_helper=None,
    )
    assert ocr_obj["text"] == "cli only" and called["cli"] == 1


def test_the_converter_hands_the_helper_an_absolute_deadline(tmp_path):
    """A duration would decay while the page waits on WarmOCR's lock.

    This asserts only the contract the converter owns -- that it passes an
    instant derived from the remaining budget. Whether the wait is actually
    deducted is a property of the LOCK, and is tested against the real one in
    test_ocr_warm.py; a fake helper here has no lock and could never show it.
    """
    import time as _time

    seen = {}

    class Recording:
        def is_ready(self):
            return True

        def ocr(self, png, *, lang, psm, timeout_s, deadline=None):
            seen["deadline"] = deadline
            seen["at"] = _time.monotonic()
            return OCRResult("warm", 3, 1)

    rec = {"file": "page-001.png", "index": 1, "width": 100, "height": 100}
    (tmp_path / "page-001.png").write_bytes(b"x")

    def cli(scan_png, *, lang, psm, timeout_s, argv_runner=None):
        raise AssertionError("the warm path must not fall back here")

    _process_page_scanners(
        tmp_path, rec, is_blank=False, qr_enabled=False, qr_formats="", qr_timeout_s=5,
        ocr_enabled=True, ocr_lang="eng", ocr_psm=3, ocr_time_left=lambda: 42.0,
        has_images=True, ocr_all=True, _ocr_fn=cli, ocr_helper=Recording(),
    )
    assert seen["deadline"] is not None, "the helper must be given a deadline"
    budget_left = seen["deadline"] - seen["at"]
    assert 41.0 < budget_left <= 42.0, (
        f"the deadline must carry the remaining 42s budget, got {budget_left}"
    )
