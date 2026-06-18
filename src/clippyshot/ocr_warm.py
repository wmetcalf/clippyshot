"""Persistent tesserocr OCR helper — the warm-OCR tier.

Tesseract has no daemon mode, so "keep the model warm" means holding a
``tesserocr.PyTessBaseAPI`` alive in a helper subprocess and feeding it one
page per request over a stdin/stdout JSON pipe, so OCR avoids re-loading the
~300 ms eng+Latin model per page. Mirrors ``clippyshot.libreoffice.uno.
UnoServer``: an injectable ``Popen`` lifecycle that unit-tests without a real
tesseract, started at ``engine.warmup()`` (warm tiers) or per-job (cold
container), and **fail-closed** — any failure raises ``OCRError`` so the caller
falls back to the cold ``tesseract`` CLI path and never fails the page.

Isolation: the helper is a subprocess (never in-process), so a malicious-PNG
libtesseract/leptonica bug stays confined exactly as the per-page CLI does.
Transport is a stdin/stdout pipe — no socket.

Concurrency + timeout: the converter OCRs pages from a thread pool, all sharing
one ``WarmOCR``. ``ocr()`` is serialised by a lock (the win is the amortised
model load, not intra-job OCR parallelism) so request/response pairs never
interleave on the single pipe. Each read is bounded by a deadline; a tesseract
hang on a crafted page can't be interrupted in C, so on timeout the helper is
**SIGKILLed** and the caller cold-falls-back — matching the per-page deadline
the sandboxed CLI enforces.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from clippyshot.ocr import (
    DEFAULT_LANG,
    DEFAULT_PSM,
    DEFAULT_TIMEOUT_S,
    OCRError,
    OCRResult,
    validate_lang,
)


class WarmOCR:
    """Lifecycle + client for the persistent tesserocr helper subprocess."""

    def __init__(
        self,
        *,
        python_bin: str = sys.executable,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        ready_timeout_s: float = 30.0,
    ) -> None:
        self._python_bin = python_bin
        self._popen = popen
        self._ready_timeout_s = ready_timeout_s
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()  # serialises ocr() over the single pipe

    def argv(self) -> list[str]:
        # Foreground module entrypoint: the Popen handle IS the server so
        # stop()/poll() control its lifecycle (mirrors UnoServer).
        return [self._python_bin, "-m", "clippyshot.ocr_warm", "serve"]

    def start(self) -> None:
        """Spawn the helper and block until it emits ``{"ready": true}`` (bounded
        by ``ready_timeout_s``). Idempotent. Raises ``OCRError`` if it never
        readies within the deadline."""
        if self._proc is not None:
            return
        proc = self._popen(
            self.argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._proc = proc
        if proc.stdout is None:
            self.stop()
            raise OCRError("warm OCR helper has no stdout pipe")
        hello = self._readline(self._ready_timeout_s)
        try:
            ready = bool(hello) and json.loads(hello).get("ready") is True
        except ValueError:
            ready = False
        if not ready:
            self.stop()
            raise OCRError("warm OCR helper did not become ready")

    def is_ready(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _readline(self, timeout_s: float) -> str:
        """Read one line from the helper, bounded by ``timeout_s``. A tesseract
        hang can't be interrupted in C, so on timeout the helper is SIGKILLed
        (``stop()``) and ``OCRError`` raised — the caller cold-falls-back."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise OCRError("warm OCR helper not ready")
        out = proc.stdout
        q: queue.Queue = queue.Queue(maxsize=1)

        def _read() -> None:
            try:
                q.put(out.readline())
            except Exception as e:  # noqa: BLE001 - pipe closed / killed
                q.put(e)

        threading.Thread(target=_read, daemon=True).start()
        try:
            item = q.get(timeout=timeout_s)
        except queue.Empty:
            self.stop()  # kill the hung helper; subsequent pages take the CLI
            raise OCRError(f"warm OCR helper timed out after {timeout_s:g}s")
        if isinstance(item, BaseException):
            raise OCRError(f"warm OCR read failed: {item}")
        return item

    def ocr(
        self,
        png: Path,
        *,
        lang: str = DEFAULT_LANG,
        psm: int = DEFAULT_PSM,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> OCRResult:
        """OCR one page via the warm helper. Serialised over the single pipe and
        bounded by ``timeout_s``. Raises ``OCRError`` on any failure (caller
        falls back to the cold CLI path)."""
        validate_lang(lang)
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None or proc.stdout is None:
                raise OCRError("warm OCR helper not ready")
            try:
                proc.stdin.write(
                    json.dumps(
                        {"png": str(png), "lang": lang, "psm": psm, "timeout_s": timeout_s}
                    )
                    + "\n"
                )
                proc.stdin.flush()
            except OSError as e:
                raise OCRError(f"warm OCR transport failure: {e}") from e
            line = self._readline(timeout_s)
            try:
                resp = json.loads(line) if line else {"error": "no response from warm OCR helper"}
            except ValueError as e:
                raise OCRError(f"warm OCR bad response: {e}") from e
            if "error" in resp:
                raise OCRError(str(resp["error"]))
            try:
                return OCRResult(
                    text=str(resp["text"]),
                    char_count=int(resp["char_count"]),
                    duration_ms=int(resp["duration_ms"]),
                )
            except (KeyError, TypeError, ValueError) as e:
                raise OCRError(f"warm OCR malformed response: {e}") from e

    def stop(self) -> None:
        """Terminate the helper: best-effort ``quit``, then SIGTERM, then SIGKILL
        after a 5 s grace. Safe on a hung helper (the small quit write can't fill
        the pipe; SIGKILL is the real teardown)."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                proc.stdin.flush()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _serve() -> None:  # pragma: no cover - runs in the helper subprocess
    """Helper-subprocess loop: load tesserocr once per (lang, psm), then serve
    one page per stdin request. A per-request failure is non-fatal (returns an
    ``error`` so the caller cold-falls-back). A hang is handled by the client
    SIGKILLing this process — tesseract C calls aren't interruptible here."""
    from tesserocr import PyTessBaseAPI  # PyTessBaseAPI takes psm as a plain int

    apis: dict[tuple[str, int], PyTessBaseAPI] = {}

    def api_for(lang: str, psm: int) -> PyTessBaseAPI:
        key = (lang, psm)
        if key not in apis:
            api = PyTessBaseAPI(lang=lang, psm=psm)
            # Match the CLI path's --dpi 150 (DEFAULT_DPI in clippyshot.ocr).
            api.SetVariable("user_defined_dpi", "150")
            apis[key] = api
        return apis[key]

    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("cmd") == "quit":
                break
            api = api_for(req.get("lang", DEFAULT_LANG), int(req.get("psm", DEFAULT_PSM)))
            t0 = time.monotonic()
            api.SetImageFile(req["png"])
            text = (api.GetUTF8Text() or "").rstrip("\n")
            resp = {
                "text": text,
                "char_count": len(text),
                "duration_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as e:  # noqa: BLE001 - non-fatal; caller cold-falls-back
            resp = {"error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _serve()
    else:
        sys.exit("usage: python -m clippyshot.ocr_warm serve")
