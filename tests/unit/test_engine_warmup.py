"""Tests for ClippyShotEngine.warmup() — the warm-tier server seam."""
from __future__ import annotations

import pytest

from clippyshot.engine import ClippyShotEngine


@pytest.fixture(autouse=True)
def _hermetic_warm_profile(tmp_path, monkeypatch):
    # warmup() writes the hardened LO profile to CLIPPYSHOT_WARM_PROFILE_DIR; redirect it to a
    # tmp dir so tests don't write into /tmp/.clippyshot-warm-profile.
    monkeypatch.setenv("CLIPPYSHOT_WARM_PROFILE_DIR", str(tmp_path / "warm-profile"))


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


def test_warmup_writes_and_passes_hardened_profile(monkeypatch, tmp_path):
    # SECURITY (H1): the warm server must boot with the hardened LO profile (macro/Basic/Java
    # lockdown), captured warm in the snapshot — not LibreOffice defaults.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    prof = tmp_path / "wp"
    monkeypatch.setenv("CLIPPYSHOT_WARM_PROFILE_DIR", str(prof))
    captured = {}

    class FakeServer:
        def start(self):
            pass

        def stop(self):
            pass

        def convert(self, *a):
            pass

    def _factory(*a, **k):
        captured["user_installation"] = k.get("user_installation")
        return FakeServer()

    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", _factory)
    eng = ClippyShotEngine()
    eng.warmup()
    xcu = prof / "user" / "registrymodifications.xcu"
    assert xcu.exists(), "warmup() must write the hardened profile before starting the server"
    body = xcu.read_text()
    assert "DisableMacrosExecution" in body and "MacroSecurityLevel" in body
    assert captured["user_installation"] == "file://%s" % prof.resolve()


def test_warmup_fails_closed_when_profile_unwritable(monkeypatch):
    # If the hardened profile can't be written, warmup() must NOT start an unhardened server —
    # it falls back to the cold path (which writes its own per-job hardened profile).
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    started = []

    class FakeServer:
        def start(self):
            started.append(True)

        def stop(self):
            pass

    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: FakeServer())

    def _boom(self):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("clippyshot.libreoffice.profile.HardenedProfile.write", _boom)
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise
    assert started == [], "must not start an unhardened warm server"
    assert eng._uno_server is None


class _PrimingServer:
    def __init__(self):
        self.primed = []

    def start(self):
        pass

    def stop(self):
        pass

    def convert(self, src, dst, label):
        # Record the priming convert + create the output (mirrors a real convert).
        self.primed.append((src.name, label))
        dst.write_bytes(b"%PDF-1.4 primed\n")


def test_warmup_primes_filters_by_default(monkeypatch):
    # The snapshot must capture a server with its conversion filters warmed, else the
    # first post-restore convert pays a multi-second cold-filter load. warmup() runs a
    # throwaway conversion per prime doc.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.delenv("CLIPPYSHOT_WARM_PRIME", raising=False)
    srv = _PrimingServer()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is srv
    assert srv.primed, "warmup() must run at least one priming conversion"
    # Default corpus warms the Writer/PDF-export path (txt → writer_pdf_Export).
    assert any(label == "txt" for _name, label in srv.primed)


def test_warmup_priming_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.setenv("CLIPPYSHOT_WARM_PRIME", "0")
    srv = _PrimingServer()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()
    assert eng._uno_server is srv
    assert srv.primed == []  # priming opt-out honored


def test_warmup_priming_failure_is_nonfatal(monkeypatch):
    # A priming-convert failure must leave the warm server in place (the first real
    # convert just pays the warmup once) — it must NOT disable the warm tier or raise.
    monkeypatch.setenv("CLIPPYSHOT_WARM_UNO", "1")
    monkeypatch.delenv("CLIPPYSHOT_WARM_PRIME", raising=False)

    class _PrimeFails(_PrimingServer):
        def convert(self, src, dst, label):
            raise RuntimeError("filter load failed")

    srv = _PrimeFails()
    monkeypatch.setattr("clippyshot.libreoffice.uno.UnoServer", lambda *a, **k: srv)
    eng = ClippyShotEngine()
    eng.warmup()  # must NOT raise
    assert eng._uno_server is srv  # warm tier stays active despite priming failure
