"""Unit tests for `run_ocr` — the subprocess wrapper around tesseract."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clippyshot.ocr import OCRResult, OCRError, run_ocr


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_ocr_happy_path(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        assert argv[0].endswith("tesseract")
        assert str(png) in argv
        assert "-" in argv
        assert "-l" in argv
        assert "--psm" in argv
        return _FakeCompleted(returncode=0, stdout="Hello OCR\nSecond line\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_ocr(png)
    assert isinstance(out, OCRResult)
    assert out.text == "Hello OCR\nSecond line"
    assert out.char_count == len("Hello OCR\nSecond line")
    assert out.duration_ms >= 0


def test_run_ocr_passes_lang_and_psm(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")
    captured = {}

    def fake_run(argv, *, capture_output, text, timeout, check):
        captured["argv"] = argv
        return _FakeCompleted(returncode=0, stdout="x")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_ocr(png, lang="eng+deu", psm=11)
    argv = captured["argv"]
    assert argv[argv.index("-l") + 1] == "eng+deu"
    assert argv[argv.index("--psm") + 1] == "11"


def test_run_ocr_empty_output_returns_result_with_empty_text(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = run_ocr(png)
    assert out.text == ""
    assert out.char_count == 0


def test_run_ocr_timeout_raises_ocr_error(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OCRError) as exc_info:
        run_ocr(png, timeout_s=2)
    assert "timeout" in str(exc_info.value).lower()


def test_run_ocr_nonzero_exit_raises_ocr_error(monkeypatch, tmp_path):
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")

    def fake_run(argv, *, capture_output, text, timeout, check):
        return _FakeCompleted(returncode=1, stdout="", stderr="corrupt image")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(OCRError) as exc_info:
        run_ocr(png)
    assert "exited 1" in str(exc_info.value)
