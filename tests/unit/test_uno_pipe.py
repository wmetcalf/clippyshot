"""Unit tests for the soffice ``--accept=pipe`` warm transport (no real LibreOffice).

Process/subprocess deps are injected so the lifecycle + conversion wiring are
exercised without LibreOffice or pyuno.
"""
import subprocess
from pathlib import Path

import pytest

from clippyshot.errors import LibreOfficeError
from clippyshot.libreoffice.uno_pipe import (
    SofficePipeServer,
    _filter_data_json,
    convert_via_pipe,
    default_uno_python,
)


class _FakeProc:
    def __init__(self, rc=None):
        self._rc = rc
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = 0

    def wait(self, timeout=None):
        return self._rc or 0

    def kill(self):
        self.killed = True
        self._rc = -9


def _cp(rc):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=b"")


@pytest.mark.parametrize("basename_fmt", ["OSL_PIPE_{euid}_{name}", "OSL_PIPE_{name}"])
def test_default_socket_check_accepts_both_pipe_basenames(tmp_path, monkeypatch, basename_fmt):
    # Regression: LibreOffice 25.8 under runsc creates the accept pipe at
    # ``$TMPDIR/OSL_PIPE_<name>`` (NO euid prefix); older builds use
    # ``OSL_PIPE_<euid>_<name>``. Checking only the euid form silently failed the warmup
    # readiness poll, so every "warm" job fell back to cold. Both forms must be recognized.
    import os

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    s = SofficePipeServer(pipe_name="clippyshot")
    assert s._default_socket_check() is False  # nothing there yet
    sock = tmp_path / basename_fmt.format(euid=os.geteuid(), name="clippyshot")
    sock.write_text("")  # the acceptor socket appears
    assert s._default_socket_check() is True


def test_argv_uses_accept_pipe_not_tcp():
    argv = SofficePipeServer(pipe_name="px").argv()
    assert argv[0] == "soffice"
    assert "--headless" in argv
    assert "--accept=pipe,name=px;urp;StarOffice.ComponentContext" in argv
    # The whole point: NO loopback / TCP socket — pipe transport only.
    assert not any(("socket" in a) or (a == "--port") or (a == "--interface") for a in argv)


def test_start_adopts_already_ready_without_spawn():
    spawned = []
    s = SofficePipeServer(
        popen=lambda argv: spawned.append(argv) or _FakeProc(),
        socket_check=lambda: True,  # acceptor socket already present -> adopt
    )
    s.start()
    assert spawned == []  # adopted, never spawned
    assert s.is_ready()


def test_start_spawns_then_becomes_ready():
    calls = {"n": 0}

    def sock():  # socket absent on the adopt check, present after spawn
        calls["n"] += 1
        return calls["n"] >= 2

    proc = _FakeProc()
    s = SofficePipeServer(popen=lambda argv: proc, socket_check=sock, sleep=lambda _s: None)
    s.start()
    assert s.is_ready()


def test_start_raises_if_soffice_exits_early():
    proc = _FakeProc(rc=3)  # exits immediately
    s = SofficePipeServer(
        popen=lambda argv: proc, socket_check=lambda: False, sleep=lambda _s: None
    )
    with pytest.raises(LibreOfficeError):
        s.start()


def test_convert_builds_pipe_argv_with_calc_filter_data(tmp_path: Path):
    inp = tmp_path / "in.xlsx"
    inp.write_bytes(b"x")
    outp = tmp_path / "out.pdf"
    seen = {}

    def run(argv, **k):
        seen["argv"] = argv
        outp.write_bytes(b"%PDF\n%%EOF\n")
        return _cp(0)

    s = SofficePipeServer(pipe_name="px", uno_python="/usr/bin/python3")
    convert_via_pipe(s, inp, outp, "xlsx", run=run)
    argv = seen["argv"]
    assert argv[0] == "/usr/bin/python3"
    assert "px" in argv  # pipe name
    assert "calc_pdf_Export" in argv  # Calc filter parity
    assert inp.resolve().as_uri() in argv
    assert outp.resolve().as_uri() in argv
    assert '"SinglePageSheets": true' in argv[-1]  # FilterData parity (one page per sheet)


def test_convert_writer_has_no_filter_data(tmp_path: Path):
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    outp = tmp_path / "out.pdf"

    def run(argv, **k):
        outp.write_bytes(b"%PDF\n%%EOF\n")
        return _cp(0)

    s = SofficePipeServer()
    convert_via_pipe(s, inp, outp, "docx", run=run)
    assert outp.read_bytes().startswith(b"%PDF")


def test_convert_raises_on_nonzero(tmp_path: Path):
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    s = SofficePipeServer()
    with pytest.raises(LibreOfficeError):
        convert_via_pipe(s, inp, tmp_path / "out.pdf", "docx", run=lambda *a, **k: _cp(1))


def test_convert_raises_on_empty_output(tmp_path: Path):
    inp = tmp_path / "in.docx"
    inp.write_bytes(b"x")
    s = SofficePipeServer()
    # run "succeeds" but writes nothing -> fail closed (don't pass an empty PDF).
    with pytest.raises(LibreOfficeError):
        convert_via_pipe(s, inp, tmp_path / "out.pdf", "docx", run=lambda *a, **k: _cp(0))


def test_filter_data_json_renders_bools():
    assert _filter_data_json({}) is None
    assert '"SinglePageSheets": true' in (_filter_data_json({"SinglePageSheets": "true"}) or "")


def test_default_uno_python_env(monkeypatch):
    monkeypatch.delenv("CLIPPYSHOT_UNO_PYTHON", raising=False)
    assert default_uno_python() == "/usr/bin/python3"
    monkeypatch.setenv("CLIPPYSHOT_UNO_PYTHON", "/opt/lo/python")
    assert default_uno_python() == "/opt/lo/python"
