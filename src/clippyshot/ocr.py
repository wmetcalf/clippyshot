"""OCR via the tesseract CLI.

Invocation pattern:
    tesseract <png> - -l <lang> --psm <psm>

The trailing `-` makes tesseract write plain text to stdout.

Designed to be called on the best-available PNG derivative for a page
(focused > trimmed > original — selection happens in converter.py).
Scanner failures raise `OCRError`; callers catch this and surface the
error as a non-fatal per-page `ocr.skipped="error"` entry.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OCRResult:
    text: str
    char_count: int
    duration_ms: int


class OCRError(RuntimeError):
    """Raised by run_ocr when tesseract failed to execute cleanly."""


# Default covers the bulk of world documents via tesseract script
# meta-models plus dedicated models for non-Latin/Cyrillic scripts.
# `Latin` covers English, German, French, Spanish, Portuguese, Italian,
# Vietnamese, Turkish, Polish, Czech, Slovak, Croatian, and others.
# `Cyrillic` covers Russian, Ukrainian, Bulgarian, Serbian, Belarusian, etc.
# The remaining entries are single-language models.
DEFAULT_LANG = "eng+Latin"
# psm=3 (fully automatic page segmentation) matches tesseract's own default
# and produces better results on office documents than psm=6 (single uniform
# block of text). Rendered office pages contain headers, multi-column layouts,
# tables, and mixed font sizes that psm=6 tends to mash together.
DEFAULT_PSM = 3
DEFAULT_TIMEOUT_S = 60
# Rasterization DPI we tell tesseract about. Our pdftoppm output is 150 DPI
# by default; tesseract assumes 300 if not told, and misinterpreting the DPI
# hurts both accuracy and speed. Keep this in sync with Limits.dpi.
DEFAULT_DPI = 150


def _tesseract_binary() -> str:
    # Prefer /usr/bin and /usr/local/bin so the path resolves inside the
    # nsjail/bwrap sandboxes (which only bind-mount /usr). PATH-first lookup
    # would return user-local installs (e.g. ~/.local/bin/tesseract) that
    # exist on the host but aren't reachable from inside the sandbox.
    for cand in ("/usr/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(cand).is_file():
            return cand
    found = shutil.which("tesseract")
    if found:
        return found
    raise OCRError("tesseract binary not installed — install the tesseract-ocr package")


def _default_runner(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
    """Plain subprocess.run — used when no sandbox runner is injected (tests)."""
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s, check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_ocr(
    png_path: Path,
    *,
    lang: str = DEFAULT_LANG,
    psm: int = DEFAULT_PSM,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    argv_runner=None,
) -> OCRResult:
    """Run tesseract on a PNG and return the extracted text.

    `argv_runner` is a callable `(argv, timeout_s) -> (exit, stdout, stderr)`.
    Defaults to plain `subprocess.run`; Converter injects a sandbox-backed
    runner so tesseract executes under the same sandbox as soffice.

    Raises `OCRError` on runner failure (non-zero exit, timeout,
    missing binary).
    """
    runner = argv_runner or _default_runner
    bin_path = _tesseract_binary()
    argv = [
        bin_path,
        str(png_path),
        "-",
        "-l", lang,
        "--psm", str(psm),
        "--dpi", str(DEFAULT_DPI),
    ]
    t0 = time.monotonic()
    try:
        exit_code, stdout, stderr = runner(argv, timeout_s)
    except subprocess.TimeoutExpired:
        raise OCRError(f"tesseract timeout after {timeout_s}s on {png_path.name}")
    except FileNotFoundError:
        raise OCRError("tesseract binary not installed — install the tesseract-ocr package")
    if exit_code != 0:
        raise OCRError(
            f"tesseract exited {exit_code} on {png_path.name}: "
            f"{stderr.strip()[:500]}"
        )
    text = (stdout or "").rstrip("\n")
    duration_ms = int((time.monotonic() - t0) * 1000)
    return OCRResult(text=text, char_count=len(text), duration_ms=duration_ms)
