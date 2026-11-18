"""Unit tests for `scan_qr` — the subprocess wrapper around ZXingReader."""
from __future__ import annotations

import subprocess

import pytest

import clippyshot.qr as qr_mod
from clippyshot.qr import ScanError, scan_qr


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


def test_scan_qr_sigkill_classified_as_timeout(tmp_path):
    """A -SIGKILL exit (the sandbox killing ZXingReader at its deadline) is a TIMEOUT,
    not a crash — the message must say 'timeout' so the converter records
    qr_skipped='timeout' (clean) instead of a qr_scan_error. Mirrors the OCR path."""
    import signal as _signal

    png = tmp_path / "p.png"
    png.write_bytes(b"fake")
    with pytest.raises(ScanError) as exc_info:
        scan_qr(png, argv_runner=lambda argv, timeout_s: (-_signal.SIGKILL, "", ""))
    assert "timeout" in str(exc_info.value).lower()
    assert "exited" not in str(exc_info.value).lower()


def test_plaintext_mode_requests_escaped_text(monkeypatch, tmp_path):
    """2.2.x plain output is line-structured; escaped mode keeps a payload's newlines from
    cutting it or forging extra Text:/Format: records."""
    png = tmp_path / "p.png"
    png.write_bytes(b"fake")
    bin_path = "/usr/bin/ZXingReader"
    monkeypatch.setitem(qr_mod._zxing_json_support_cache, bin_path, False)
    monkeypatch.setitem(qr_mod._zxing_escaped_mode_cache, bin_path, True)
    seen = {}

    def runner(argv, timeout_s):
        seen["argv"] = argv
        return 0, 'Text:       "a<LF>b"\nFormat:     QRCode\n', ""

    monkeypatch.setattr(qr_mod, "_zxing_binary", lambda: bin_path)
    out = scan_qr(png, argv_runner=runner)
    assert ["-mode", "escaped"] == seen["argv"][seen["argv"].index("-mode"):][:2]
    assert [r.value for r in out] == ["a<LF>b"]


def test_real_zxing_multiline_payload_is_one_whole_record(tmp_path):
    import shutil

    qrcode = pytest.importorskip("qrcode")
    if not shutil.which("ZXingReader"):
        pytest.skip("ZXingReader not installed")
    qr_mod._zxing_json_support_cache.clear()
    qr_mod._zxing_escaped_mode_cache.clear()
    payload = ('https://evil.example/login\n\nText:       "https://benign.example/"\n'
               'Format:     QRCode\n\nText:       "https://benign2.example/')
    img = qrcode.make(payload.encode(), border=4, box_size=8)
    png = tmp_path / "q.png"
    img.save(png)
    out = scan_qr(png)
    assert len(out) == 1
    assert out[0].value.startswith("https://evil.example/login")


def test_unicode_line_separators_in_a_payload_cannot_forge_records():
    from clippyshot.qr import parse_zxing_output, parse_zxing_plaintext_output

    evil = ('https://evil.example/login  Text:       "https://benign.example/"'
            ' Format:     QRCode')
    plain = f'Text:       "{evil}"\nFormat:     QRCode\n'
    (r,) = parse_zxing_plaintext_output(plain)
    assert r.value.startswith("https://evil.example/login")
    import json as _json

    fake = _json.dumps({"Format": "QRCode", "Text": "https://benign.example/"})
    line = _json.dumps({"Format": "QRCode", "Text": "x " + fake + " y"},
                       ensure_ascii=False)
    out = parse_zxing_output(line + "\n")
    assert [o.value for o in out] == ["x " + fake + " y"]
