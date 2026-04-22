"""Unit tests for `scan_qr` — the subprocess wrapper around ZXingReader."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import clippyshot.qr as qr_mod
from clippyshot.qr import QRResult, ScanError, scan_qr


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _force_json_mode(monkeypatch, bin_path: str = "/usr/bin/ZXingReader") -> None:
    """Pre-populate the json-support cache so unit tests don't need to call -help."""
    monkeypatch.setitem(qr_mod._zxing_json_support_cache, bin_path, True)


def test_scan_qr_happy_path(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    _force_json_mode(monkeypatch)

    def fake_run(argv, *, capture_output, text, timeout, check):
        assert argv[0].endswith("ZXingReader")
        assert "-json" in argv and "-fast" in argv
        assert str(png) in argv
        return _FakeCompleted(
            returncode=0,
            stdout='{"Format":"QRCode","Text":"https://x"}\n',
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = scan_qr(png)
    assert len(out) == 1
    assert out[0].format == "qr_code"
    assert out[0].value == "https://x"


def test_scan_qr_custom_formats_arg(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")
    captured = {}

    _force_json_mode(monkeypatch)

    def fake_run(argv, *, capture_output, text, timeout, check):
        captured["argv"] = argv
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scan_qr(png, formats="qr_code,data_matrix")
    argv = captured["argv"]
    # In JSON mode (>= 2.3) the flag is -formats; verify it's present and correct
    i = argv.index("-formats")
    assert argv[i + 1] == "qr_code,data_matrix"


def test_scan_qr_timeout_raises_scan_error(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ScanError) as exc_info:
        scan_qr(png, timeout_s=2)
    assert "timeout" in str(exc_info.value).lower()


def test_scan_qr_nonzero_exit_raises_scan_error(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        return _FakeCompleted(returncode=2, stdout="", stderr="bad image")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ScanError) as exc_info:
        scan_qr(png)
    assert "exited 2" in str(exc_info.value)
    assert "bad image" in str(exc_info.value)


def test_scan_qr_missing_binary_raises_scan_error(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        raise FileNotFoundError("ZXingReader")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ScanError) as exc_info:
        scan_qr(png)
    assert "not installed" in str(exc_info.value).lower()
