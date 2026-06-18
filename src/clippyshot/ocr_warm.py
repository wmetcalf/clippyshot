"""Persistent tesserocr OCR helper — the warm-OCR tier.

Tesseract has no daemon mode, so "keep the model warm" means holding a
``tesserocr.PyTessBaseAPI`` alive in a helper subprocess and feeding it one
page per request over a pipe. This mirrors ``clippyshot.libreoffice.uno.
UnoServer``: an injectable ``Popen`` lifecycle that unit-tests without a real
tesseract, started at ``engine.warmup()`` (warm tiers) or per-job (cold
container), and **fail-closed** — any failure raises ``OCRError`` so the caller
falls back to the cold ``tesseract`` CLI path and never fails the page.

Isolation: the helper is a subprocess (never in-process), so a malicious-PNG
libtesseract/leptonica bug stays confined exactly as the per-page CLI does.
Transport is a stdin/stdout pipe — no socket, strictly less attack surface than
warm-UNO's loopback URP socket.
"""
from __future__ import annotations

import json
import subprocess
import sys
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

    def argv(self) -> list[str]:
        # Foreground module entrypoint: the Popen handle IS the server so
        # stop()/poll() control its lifecycle (mirrors UnoServer).
        return [self._python_bin, "-m", "clippyshot.ocr_warm", "serve"]

    def start(self) -> None:
        """Spawn the helper and block until it emits its ``{"ready": true}``
        line. Idempotent. Raises ``OCRError`` if it never readies."""
        if self._proc is not None:
            return
        self._proc = self._popen(
            self.argv(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        hello = self._proc.stdout.readline()
        try:
            ready = bool(hello) and json.loads(hello).get("ready") is True
        except ValueError:
            ready = False
        if not ready:
            self.stop()
            raise OCRError("warm OCR helper did not become ready")

    def is_ready(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def ocr(
        self,
        png: Path,
        *,
        lang: str = DEFAULT_LANG,
        psm: int = DEFAULT_PSM,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> OCRResult:
        """OCR one page via the warm helper. Raises ``OCRError`` on any failure
        (caller falls back to the cold CLI path)."""
        if not self.is_ready():
            raise OCRError("warm OCR helper not ready")
        validate_lang(lang)
        try:
            self._proc.stdin.write(
                json.dumps(
                    {"png": str(png), "lang": lang, "psm": psm, "timeout_s": timeout_s}
                )
                + "\n"
            )
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            resp = json.loads(line) if line else {"error": "no response from warm OCR helper"}
        except (OSError, ValueError) as e:
            raise OCRError(f"warm OCR transport failure: {e}") from e
        if "error" in resp:
            raise OCRError(str(resp["error"]))
        return OCRResult(
            text=resp["text"],
            char_count=resp["char_count"],
            duration_ms=resp["duration_ms"],
        )

    def stop(self) -> None:
        """Terminate the helper (SIGTERM, then SIGKILL after a 5 s grace)."""
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


def _serve() -> None:  # pragma: no cover - runs in the helper subprocess
    """Helper-subprocess loop: load tesserocr once per language, then serve one
    page per stdin request. A per-request failure is non-fatal (returns an
    ``error`` so the caller cold-falls-back)."""
    from tesserocr import PSM, PyTessBaseAPI

    apis: dict[str, PyTessBaseAPI] = {}

    def api_for(lang: str) -> PyTessBaseAPI:
        if lang not in apis:
            api = PyTessBaseAPI(lang=lang, psm=PSM.AUTO)
            # Match the CLI path's --dpi 150 (DEFAULT_DPI in clippyshot.ocr).
            api.SetVariable("user_defined_dpi", "150")
            apis[lang] = api
        return apis[lang]

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
            api = api_for(req.get("lang", DEFAULT_LANG))
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
