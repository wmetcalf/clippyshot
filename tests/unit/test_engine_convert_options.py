"""Unit guards for ClippyShotEngine._convert_options_from_env.

The engine maps the per-job CLIPPYSHOT_* scanner toggles (QR/OCR/limits) — which
the dispatcher forwards to cold workers and the blastbox warm worker injects into
os.environ before detonate — onto ConvertOptions. These lock that mapping so a
regression (a toggle silently ignored, a clamp dropped, a malformed param not
falling back) fails in CI instead of only in a live convert on some tier.
"""
from __future__ import annotations

import types

import pytest

from clippyshot.engine import ClippyShotEngine

_SCANNER_ENV = (
    "CLIPPYSHOT_QR", "CLIPPYSHOT_QR_FORMATS", "CLIPPYSHOT_QR_TIMEOUT_S",
    "CLIPPYSHOT_OCR", "CLIPPYSHOT_OCR_ALL", "CLIPPYSHOT_OCR_LANG",
    "CLIPPYSHOT_OCR_PSM", "CLIPPYSHOT_OCR_TIMEOUT_S",
)


def _opts(monkeypatch, **env):
    for k in _SCANNER_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return ClippyShotEngine._convert_options_from_env(types.SimpleNamespace(timeout_s=30))


def test_defaults_when_no_scanner_env(monkeypatch):
    o = _opts(monkeypatch)
    assert o.qr_enabled is True            # framework-proof default: QR on
    assert o.ocr_enabled is False          #                         OCR off
    assert o.ocr_all is False
    assert o.qr_formats == "qr_code,micro_qr_code,rmqr_code"
    assert o.qr_timeout_s == 10
    assert o.ocr_psm == 3
    assert o.ocr_lang == "eng+Latin"


def test_explicit_toggles_honoured(monkeypatch):
    o = _opts(monkeypatch, CLIPPYSHOT_QR="0", CLIPPYSHOT_OCR="1", CLIPPYSHOT_OCR_ALL="true")
    assert o.qr_enabled is False           # per-job OFF beats the on-default
    assert o.ocr_enabled is True           # per-job ON beats the off-default
    assert o.ocr_all is True


@pytest.mark.parametrize("truthy", ["1", "true", "YES", "On"])
def test_flag_truthy_spellings(monkeypatch, truthy):
    assert _opts(monkeypatch, CLIPPYSHOT_OCR=truthy).ocr_enabled is True


def test_numeric_params_are_clamped(monkeypatch):
    o = _opts(
        monkeypatch,
        CLIPPYSHOT_QR_TIMEOUT_S="99999",   # > 120 hi
        CLIPPYSHOT_OCR_TIMEOUT_S="0",      # < 1 lo
        CLIPPYSHOT_OCR_PSM="99",           # > 13 hi
    )
    assert o.qr_timeout_s == 120
    assert o.ocr_timeout_s == 1
    assert o.ocr_psm == 13


def test_malformed_params_fall_back(monkeypatch):
    o = _opts(
        monkeypatch,
        CLIPPYSHOT_QR_FORMATS="BAD;;;NOPE",   # not [a-z0-9_] csv → default
        CLIPPYSHOT_OCR_PSM="abc",             # non-int → 3
        CLIPPYSHOT_OCR_LANG="../etc/passwd",  # not [A-Za-z0-9_+-] → default
    )
    assert o.qr_formats == "qr_code,micro_qr_code,rmqr_code"
    assert o.ocr_psm == 3
    assert o.ocr_lang == "eng+Latin"
