"""Unit tests for the warm-UNO conversion path (no real LibreOffice needed)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clippyshot.errors import LibreOfficeError
from clippyshot.libreoffice.uno import (
    UnoServer,
    convert_via_uno,
    pdf_filter_for_label,
    unoconvert_argv,
)

# ---------------------------------------------------------------------------
# Pure filter mapping — MUST stay in lockstep with LibreOfficeRunner so warm
# output is identical to the cold --convert-to path.
# ---------------------------------------------------------------------------

_CALC = ("calc_pdf_Export", {"SinglePageSheets": "true"})


@pytest.mark.parametrize(
    "label,expected",
    [
        ("xlsx", _CALC), ("xlsm", _CALC), ("xls", _CALC), ("xlsb", _CALC),
        ("ods", _CALC), ("fods", _CALC), ("csv", _CALC),
        ("pptx", ("impress_pdf_Export", {})), ("ppt", ("impress_pdf_Export", {})),
        ("pps", ("impress_pdf_Export", {})), ("odp", ("impress_pdf_Export", {})),
        ("odg", ("draw_pdf_Export", {})), ("fodg", ("draw_pdf_Export", {})),
        ("xps", ("draw_pdf_Export", {})), ("oxps", ("draw_pdf_Export", {})),
        ("docx", ("writer_pdf_Export", {})), ("doc", ("writer_pdf_Export", {})),
        ("rtf", ("writer_pdf_Export", {})), ("odt", ("writer_pdf_Export", {})),
        ("unknown", ("writer_pdf_Export", {})), ("", ("writer_pdf_Export", {})),
    ],
)
def test_pdf_filter_for_label(label, expected):
    assert pdf_filter_for_label(label) == expected


def test_pdf_filter_case_insensitive():
    assert pdf_filter_for_label("XLSX") == _CALC
    assert pdf_filter_for_label("PpTx")[0] == "impress_pdf_Export"


# ---------------------------------------------------------------------------
# unoconvert argv — the exact invocation shape the spike validated.
# ---------------------------------------------------------------------------


def test_unoconvert_argv_calc_includes_singlepagesheets():
    argv = unoconvert_argv("unoconvert", Path("/in/a.xlsx"), Path("/out/a.pdf"), "xlsx", port=2003)
    assert argv[0] == "unoconvert"
    assert argv[argv.index("--convert-to") + 1] == "pdf"
    assert argv[argv.index("--filter") + 1] == "calc_pdf_Export"
    assert argv[argv.index("--filter-options") + 1] == "SinglePageSheets=true"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "2003"
    assert argv[-2:] == ["/in/a.xlsx", "/out/a.pdf"]


def test_unoconvert_argv_writer_has_no_filter_options():
    argv = unoconvert_argv("unoconvert", Path("/in/a.docx"), Path("/out/a.pdf"), "docx")
    assert "--filter-options" not in argv
    assert argv[argv.index("--filter") + 1] == "writer_pdf_Export"


def test_unoconvert_argv_threads_server_host_port():
    argv = unoconvert_argv("uc", Path("i"), Path("o"), "docx", host="127.0.0.1", port=9999)
    assert argv[argv.index("--port") + 1] == "9999"


# ---------------------------------------------------------------------------
# UnoServer lifecycle — injected fakes, no real process/socket.
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class _PortGate:
    """Returns False for the first ``true_after`` calls, then True forever."""

    def __init__(self, true_after=0):
        self.calls = 0
        self.true_after = true_after

    def __call__(self, host, port):
        self.calls += 1
        return self.calls > self.true_after


def test_unoserver_argv_loopback_only_foreground():
    argv = UnoServer("unoserver", host="127.0.0.1", port=2003).argv()
    assert "--daemon" not in argv  # foreground so the Popen handle IS the server
    assert argv[argv.index("--interface") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "2003"


def test_unoserver_argv_includes_user_installation_when_set():
    argv = UnoServer(user_installation="/tmp/uno").argv()
    assert argv[argv.index("--user-installation") + 1] == "/tmp/uno"


def test_start_spawns_and_waits_when_nothing_running():
    proc = _FakeProc()
    spawned: list = []
    gate = _PortGate(true_after=2)  # adopt-check False, one failed poll, then up
    sleeps: list[float] = []
    server = UnoServer(
        popen=lambda argv: (spawned.append(argv), proc)[1],
        port_check=gate,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,  # never advances → loop exits only when port is up
    )
    server.start()
    assert len(spawned) == 1          # spawned exactly one foreground unoserver
    assert sleeps == [0.1]            # polled once before it came up
    assert server.is_ready() is True


def test_start_adopts_already_running_server_without_spawning():
    spawned: list = []
    server = UnoServer(
        popen=lambda argv: (spawned.append(argv), _FakeProc())[1],
        port_check=lambda h, p: True,   # already serving (snapshot / rootfs-started)
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )
    server.start()
    assert spawned == []                # adopted; did NOT spawn a second server
    assert server.is_ready() is True
    server.stop()                       # no-op (we don't own the adopted process)


def test_start_raises_if_unoserver_exits_early():
    proc = _FakeProc(rc=1)  # already dead
    server = UnoServer(
        popen=lambda argv: proc,
        port_check=lambda h, p: False,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )
    with pytest.raises(LibreOfficeError, match="exited during warmup"):
        server.start()


def test_start_times_out_when_port_never_listens():
    clock = iter([0.0, 0.0, 31.0])  # start, first loop check, then past the 30s deadline
    server = UnoServer(
        ready_timeout_s=30.0,
        popen=lambda argv: _FakeProc(),
        port_check=lambda h, p: False,
        sleep=lambda s: None,
        monotonic=lambda: next(clock),
    )
    with pytest.raises(LibreOfficeError, match="not ready"):
        server.start()


def test_start_is_idempotent_after_spawn():
    spawned = []
    gate = _PortGate(true_after=1)  # adopt-check False, first readiness poll True
    server = UnoServer(
        popen=lambda argv: (spawned.append(1), _FakeProc())[1],
        port_check=gate,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )
    server.start()
    server.start()
    assert len(spawned) == 1  # second start is a no-op (we already own the proc)


def test_stop_terminates_a_spawned_server():
    proc = _FakeProc()
    gate = _PortGate(true_after=1)  # adopt-check False → spawn; then ready
    server = UnoServer(
        popen=lambda argv: proc,
        port_check=gate,
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )
    server.start()
    server.stop()
    assert proc.poll() == -15        # SIGTERM delivered to the spawned server


# ---------------------------------------------------------------------------
# convert_via_uno — success + fail-closed (so the caller can fall back to cold).
# ---------------------------------------------------------------------------


def _server():
    return UnoServer(host="127.0.0.1", port=2003)


def test_convert_via_uno_success(tmp_path):
    out = tmp_path / "a.pdf"

    def fake_run(argv, **kw):
        out.write_bytes(b"%PDF-1.7 ...")  # non-empty output
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    convert_via_uno(_server(), tmp_path / "a.xlsx", out, "xlsx", run=fake_run)
    assert out.is_file()


def test_convert_via_uno_nonzero_exit_raises(tmp_path):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 3, b"", b"UNO bridge gone")

    with pytest.raises(LibreOfficeError, match="rc=3.*UNO bridge gone"):
        convert_via_uno(_server(), tmp_path / "a.docx", tmp_path / "a.pdf", "docx", run=fake_run)


def test_convert_via_uno_empty_output_raises(tmp_path):
    out = tmp_path / "a.pdf"

    def fake_run(argv, **kw):
        out.write_bytes(b"")  # zero-byte → must fail closed
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    with pytest.raises(LibreOfficeError, match="no output"):
        convert_via_uno(_server(), tmp_path / "a.docx", out, "docx", run=fake_run)


def test_convert_via_uno_timeout_raises(tmp_path):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 120.0)

    with pytest.raises(LibreOfficeError, match="timed out"):
        convert_via_uno(_server(), tmp_path / "a.docx", tmp_path / "a.pdf", "docx", run=fake_run)


# ---------------------------------------------------------------------------
# PR-review hardening: loopback enforcement, OSError fallback, stale-output.
# ---------------------------------------------------------------------------


def test_unoserver_rejects_non_loopback_host():
    with pytest.raises(ValueError, match="loopback only"):
        UnoServer(host="0.0.0.0")
    with pytest.raises(ValueError, match="loopback only"):
        UnoServer(host="10.0.0.5")
    UnoServer(host="127.0.0.1")  # ok
    UnoServer(host="localhost")  # ok


def test_convert_via_uno_oserror_wrapped_as_libreoffice_error(tmp_path):
    def boom(argv, **kw):
        raise FileNotFoundError("unoconvert: command not found")

    with pytest.raises(LibreOfficeError, match="failed to execute unoconvert"):
        convert_via_uno(UnoServer(), tmp_path / "a.docx", tmp_path / "a.pdf", "docx", run=boom)


def test_convert_via_uno_unlinks_stale_output(tmp_path):
    out = tmp_path / "a.pdf"
    out.write_bytes(b"STALE")  # pre-existing output from a prior run

    seen = {}

    def fake_run(argv, **kw):
        seen["existed_at_run"] = out.exists()  # should be False (unlinked first)
        out.write_bytes(b"%PDF new")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    convert_via_uno(UnoServer(), tmp_path / "a.docx", out, "docx", run=fake_run)
    assert seen["existed_at_run"] is False
    assert out.read_bytes() == b"%PDF new"
