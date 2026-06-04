"""Tests for ClippyShotEngine.warmup() — the warm-tier server seam."""
from __future__ import annotations

from clippyshot.engine import ClippyShotEngine


def test_warmup_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_WARM_UNO", raising=False)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is None


def test_warmup_noop_when_env_falsey(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "0")
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is None


def test_warmup_starts_server_when_enabled(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    started = []

    class FakeServer:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FakeServer()
    )
    eng = ClippyShotEngine()
    eng.warmup()
    assert started == [True]
    assert eng._uno_server is not None


def test_warmup_is_nonfatal_on_start_failure(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")

    class FailingServer:
        def start(self):
            raise RuntimeError("no soffice in this env")

    monkeypatch.setattr(
        "clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FailingServer()
    )
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise — falls back to cold
    assert eng._uno_server is None
