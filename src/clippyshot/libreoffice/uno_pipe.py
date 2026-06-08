"""Warm-soffice over a UNO **pipe** — the gVisor C/R warm transport.

The TCP ``unoserver`` path (``uno.py``) needs loopback, which a bare ``runsc``
bundle lacks (no CNI configures ``lo``; gVisor doesn't implement ``SIOCSIFADDR``),
and its acceptor does not survive gVisor checkpoint/restore. A warm
``soffice --accept=pipe`` (Unix-domain pipe) needs **no network** and — with the
accept-retry ``LD_PRELOAD`` shim (``deploy/gvisor/accept_retry.c``) — **survives
C/R**: the shim retries the acceptor's EINTR-interrupted ``accept()`` on restore;
the client just retries ``connect()``. Conversion is byte-identical to the cold
``soffice --convert-to`` path (same LibreOffice engine + the same
``pdf_filter_for_label`` export filters), validated on toolz2 under ``runsc``.

The pyuno conversion itself runs in ``_pipe_convert.py`` under the LibreOffice
interpreter (the ClippyShot venv has no ``uno`` module); see that file.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from clippyshot.errors import LibreOfficeError
from clippyshot.libreoffice.uno import pdf_filter_for_label

# The pyuno client script (executed under the LibreOffice interpreter, NOT imported).
_CLIENT = Path(__file__).resolve().parent / "_pipe_convert.py"
DEFAULT_PIPE_NAME = "clippyshot"


def default_uno_python() -> str:
    """Interpreter that can ``import uno``. The ClippyShot venv interpreter CANNOT;
    the distro ``python3-uno`` (``/usr/bin/python3``) can. Override via
    ``CLIPPYSHOT_UNO_PYTHON`` when LibreOffice ships its own interpreter."""
    return os.environ.get("CLIPPYSHOT_UNO_PYTHON") or "/usr/bin/python3"


class SofficePipeServer:
    """A persistent ``soffice --accept=pipe,name=<pipe>`` (UDS; loopback-free).

    Started once in ``warmup()`` (pre-input, no untrusted data present), **adopted**
    if already running (the FC/gVisor snapshot captured a warm soffice), and torn
    down with the disposable slot. The pipe is process-local — never shared across
    documents. All process/time dependencies are injectable so the lifecycle is
    unit-tested without a real LibreOffice.
    """

    def __init__(
        self,
        soffice_bin: str = "soffice",
        *,
        pipe_name: str = DEFAULT_PIPE_NAME,
        user_installation: str | None = None,
        uno_python: str | None = None,
        ready_timeout_s: float = 30.0,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        socket_check: Callable[[], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bin = soffice_bin
        self._pipe = pipe_name
        # soffice MUST have a writable profile dir. Under a --read-only worker rootfs its
        # default (~/.config) is unwritable, so default to a tmpfs path (one pipe per slot).
        # Without this, soffice never finishes booting and the pipe acceptor never listens.
        self._user_installation = user_installation or f"file:///tmp/.clippyshot-uno-{pipe_name}"
        self._uno_python = uno_python or default_uno_python()
        self._ready_timeout_s = ready_timeout_s
        self._popen = popen
        # Readiness = the OSL named-pipe socket file exists (the acceptor is listening). This is
        # a near-zero-cost stat() — deliberately NOT a pyuno connect: spawning python+pyuno every
        # poll starves soffice's own boot under 1 CPU / runsc, so it never finishes (the acceptor
        # never appears and the poll never converges). The convert client owns the real connect
        # (it retries), so the file is a sufficient ready signal.
        self._socket_check = socket_check or self._default_socket_check
        self._sleep = sleep
        self._monotonic = monotonic
        self._proc: subprocess.Popen | None = None

    def _default_socket_check(self) -> bool:
        # soffice creates the named accept pipe at ``$TMPDIR/OSL_PIPE_<euid>_<name>``.
        tmpdir = os.environ.get("TMPDIR", "/tmp")
        return os.path.exists(os.path.join(tmpdir, f"OSL_PIPE_{os.geteuid()}_{self._pipe}"))

    @property
    def pipe_name(self) -> str:
        return self._pipe

    @property
    def uno_python(self) -> str:
        return self._uno_python

    def argv(self) -> list[str]:
        # Mirror the cold LibreOfficeRunner soffice flags, swapping --convert-to for
        # a persistent --accept=pipe. Foreground (NOT a daemon) so the Popen handle
        # IS the server and stop()/poll() control its lifecycle.
        argv = [
            self._bin,
            "--headless",
            "--invisible",
            "--nocrashreport",
            "--nodefault",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
        ]
        if self._user_installation:
            argv.append(f"-env:UserInstallation={self._user_installation}")
        argv.append(f"--accept=pipe,name={self._pipe};urp;StarOffice.ComponentContext")
        return argv

    def start(self) -> None:
        """Ensure a warm soffice pipe acceptor is listening. Idempotent.

        Adopts an already-listening acceptor (snapshot / rootfs-started) without a
        second spawn; otherwise spawns soffice and blocks until the pipe accepts a
        connection. Raises :class:`LibreOfficeError` if a spawned soffice exits early
        or never becomes ready."""
        if self._proc is not None:
            return
        if self._socket_check():
            return  # adopt an already-running soffice (snapshot / rootfs case)
        self._proc = self._popen(self.argv())
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = self._monotonic() + self._ready_timeout_s
        while self._monotonic() < deadline:
            rc = self._proc.poll() if self._proc is not None else None
            if rc is not None:
                self._proc = None
                raise LibreOfficeError(f"soffice --accept=pipe exited during warmup (rc={rc})")
            if self._socket_check():
                return
            self._sleep(0.25)
        self.stop()
        raise LibreOfficeError(
            f"soffice pipe {self._pipe!r} not ready within {self._ready_timeout_s}s"
        )

    def is_ready(self) -> bool:
        if self._proc is not None and self._proc.poll() is not None:
            return False
        return self._socket_check()

    def stop(self) -> None:
        """Terminate soffice (SIGTERM, then SIGKILL after a 5 s grace period)."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def convert(
        self, input_path: Path, output_path: Path, label: str, *, timeout_s: float = 120.0
    ) -> None:
        """Convert one document via this warm soffice pipe (the polymorphic warm-
        converter entry point; ``UnoServer`` has the same signature). Honors the
        cold-fallback contract of :func:`convert_via_pipe` — raises on any hiccup."""
        convert_via_pipe(self, input_path, output_path, label, timeout_s=timeout_s)


def _filter_data_json(options: Mapping[str, str]) -> str | None:
    """Render ``pdf_filter_for_label`` options as a JSON FilterData object for the
    pyuno client. LibreOffice filter-option string ``"true"``/``"false"`` -> JSON
    bool so the client emits a typed ``PropertyValue`` (e.g. Calc SinglePageSheets)."""
    if not options:
        return None
    out: dict[str, object] = {}
    for key, value in options.items():
        if isinstance(value, str) and value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return json.dumps(out)


def convert_via_pipe(
    server: SofficePipeServer,
    input_path: Path,
    output_path: Path,
    label: str,
    *,
    timeout_s: float = 120.0,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Convert ``input_path`` -> ``output_path`` (PDF) via the warm soffice pipe.

    Raises :class:`LibreOfficeError` on non-zero exit, timeout, or missing/empty
    output so the caller can fall back to the cold ``--convert-to`` path — a warm
    hiccup must never fail the job. The caller owns that fallback."""
    filter_name, options = pdf_filter_for_label(label)
    argv = [
        server.uno_python,
        str(_CLIENT),
        server.pipe_name,
        Path(input_path).resolve().as_uri(),
        Path(output_path).resolve().as_uri(),
        filter_name,
    ]
    filter_data = _filter_data_json(options)
    if filter_data is not None:
        argv.append(filter_data)

    # Fail closed: drop stale output so a no-op/partial conversion can't pass the
    # non-empty check below as a false success.
    if output_path.exists():
        output_path.unlink()
    try:
        proc = run(argv, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise LibreOfficeError(f"pipe convert timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise LibreOfficeError(f"failed to execute pipe converter: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr or b""
        detail = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
        raise LibreOfficeError(f"pipe convert failed (rc={proc.returncode}): {detail[:500]}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise LibreOfficeError("pipe convert produced no output PDF")
