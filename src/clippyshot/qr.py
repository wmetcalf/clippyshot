"""QR/barcode detection via the zxing-cpp CLI (`ZXingReader`).

Invocation pattern:
    ZXingReader -json -fast -norotate -noscale -formats <formats> <png>

Output is JSON-lines, one object per detected code. We hand-roll the
parser rather than using the `json` module to stay robust against
attacker-influenced inputs (malicious QR payloads appearing in the
Text field) — in particular we refuse to coerce `null` into the string
"null" or the literal `true`/`false` into any specific Python bool,
leaving that to the caller.

Ported from tika's `ZXingCPPScanner.parseJsonLine` (office-links branch).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QRResult:
    format: str                       # normalized: "qr_code", "micro_qr_code", "rmqr_code", "data_matrix", ...
    value: str                        # decoded text payload (may be "")
    position: str | None              # "x1,y1 x2,y2 x3,y3 x4,y4" or None
    error_correction_level: str | None
    is_mirrored: bool
    raw_bytes_hex: str | None


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_format(fmt: str) -> str:
    """`QRCode` → `qr_code`, `RMQRCode` → `rmqr_code`, etc."""
    if not fmt:
        return ""
    s = fmt.strip()
    # Insert underscore between a run of uppercase letters and a following
    # uppercase+lowercase pair: e.g. "QRCode" → "QR_Code"
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    # Insert underscore between a lowercase/digit and an uppercase letter:
    # e.g. "dataMatrix" → "data_Matrix"
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = s.lower()
    s = _NORMALIZE_RE.sub("_", s)
    s = s.strip("_")
    return s


def _parse_json_line(line: str) -> dict[str, str | None]:
    """Parse a single ZXingReader JSON object line.

    Strict: only accepts `{"key": value, ...}` shape with string keys.
    Values may be quoted strings (with `\"`, `\\`, `\n`, `\r`, `\t`
    escapes), `null`, or bare tokens (`true`, `false`, numbers) which
    are returned as their literal string form.

    Returns a dict mapping key → string or None (for `null`).
    """
    if not line or not (line.startswith("{") and line.endswith("}")):
        raise ValueError(f"expected JSON object: {line!r}")
    out: dict[str, str | None] = {}
    i = 1
    n = len(line) - 1
    while i < n:
        while i < n and line[i] in " \t,":
            i += 1
        if i >= n:
            break
        if line[i] != '"':
            raise ValueError(f"expected string key at pos {i} in {line!r}")
        key, i = _read_string(line, i)
        while i < len(line) and line[i] in " \t":
            i += 1
        if i >= len(line) or line[i] != ":":
            raise ValueError(f"expected ':' after key at pos {i} in {line!r}")
        i += 1
        while i < len(line) and line[i] in " \t":
            i += 1
        if i >= len(line):
            raise ValueError(f"unexpected end of input in {line!r}")
        if line[i] == '"':
            val, i = _read_string(line, i)
            out[key] = val
        else:
            start = i
            while i < n and line[i] not in ",}":
                i += 1
            token = line[start:i].strip()
            out[key] = None if token == "null" else token
    return out


def _read_string(line: str, i: int) -> tuple[str, int]:
    """Read a JSON string starting at line[i] (must be '"'). Returns (value, next_index)."""
    if line[i] != '"':
        raise ValueError(f"expected '\"' at pos {i}")
    i += 1
    buf: list[str] = []
    while i < len(line):
        ch = line[i]
        if ch == '"':
            return "".join(buf), i + 1
        if ch == "\\":
            i += 1
            if i >= len(line):
                raise ValueError("unterminated escape")
            esc = line[i]
            if esc == "n": buf.append("\n")
            elif esc == "r": buf.append("\r")
            elif esc == "t": buf.append("\t")
            elif esc in ('"', "\\", "/"): buf.append(esc)
            else: buf.append(esc)
            i += 1
            continue
        buf.append(ch)
        i += 1
    raise ValueError("unterminated string")


def parse_zxing_output(stdout: str) -> list[QRResult]:
    """Parse ZXingReader `-json` output into a list of results.

    Lines without a usable `Format` are skipped (zxing sometimes emits
    empty-result records). Never raises on per-line parse errors — bad
    lines are dropped. Returns an empty list on empty input.
    """
    results: list[QRResult] = []
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = _parse_json_line(line)
        except ValueError:
            continue
        fmt = _normalize_format(obj.get("Format") or "")
        if not fmt:
            continue
        is_mirrored_raw = obj.get("IsMirrored") or "false"
        is_mirrored = is_mirrored_raw.lower() == "true"
        results.append(
            QRResult(
                format=fmt,
                value=obj.get("Text") or "",
                position=obj.get("Position"),
                error_correction_level=obj.get("ECLevel"),
                is_mirrored=is_mirrored,
                raw_bytes_hex=obj.get("Bytes"),
            )
        )
    return results


import shutil
import subprocess
from pathlib import Path


class ScanError(RuntimeError):
    """Raised by scan_qr when ZXingReader failed to execute cleanly."""


_ZXING_BIN_CANDIDATES = ("ZXingReader", "/usr/bin/ZXingReader", "/usr/local/bin/ZXingReader")


def _zxing_binary() -> str:
    """Return the first ZXingReader path that exists on $PATH or absolute paths.

    Prefer /usr/bin and /usr/local/bin so the path resolves inside the
    nsjail/bwrap sandboxes (which only bind-mount /usr). PATH-first lookup
    would return user-local installs that exist on the host but aren't
    reachable from inside the sandbox.
    """
    for cand in _ZXING_BIN_CANDIDATES[1:]:
        if Path(cand).is_file():
            return cand
    found = shutil.which("ZXingReader")
    if found:
        return found
    raise ScanError(
        "ZXingReader binary not installed — install the zxing-cpp package"
    )


_zxing_json_support_cache: dict[str, bool] = {}


def _zxing_supports_json(bin_path: str, runner=None) -> bool:
    """Return True if this ZXingReader build accepts the -json flag (>= 2.3).

    The -help probe runs through the supplied ``runner`` when available, so
    the capability check executes inside the same sandbox as the actual
    scan — keeping every ZXingReader invocation on attacker-influenced
    hosts behind the sandbox boundary. When no runner is provided (tests,
    initial probe on an empty cache), falls back to a direct subprocess —
    -help does not read any input files, so the attack surface is null.

    Result is cached per binary path so unit tests can pre-populate the
    cache (e.g. ``_zxing_json_support_cache[bin] = True``) without needing
    to intercept the probe.
    """
    if bin_path in _zxing_json_support_cache:
        return _zxing_json_support_cache[bin_path]
    try:
        if runner is not None:
            exit_code, stdout, stderr = runner([bin_path, "-help"], 5)
            help_text = stdout + stderr
            result = exit_code == 0 and "-json" in help_text
        else:
            proc = subprocess.run(
                [bin_path, "-help"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            help_text = (proc.stdout or "") + (proc.stderr or "")
            result = "-json" in help_text
    except Exception:
        result = False
    _zxing_json_support_cache[bin_path] = result
    return result


# Format name map: ZXingReader 2.2.x plain-text → normalized internal name
_PLAINTEXT_FORMAT_MAP: dict[str, str] = {
    "qrcode": "qr_code",
    "microqrcode": "micro_qr_code",
    "rmqrcode": "rmqr_code",
    "datamatrix": "data_matrix",
    "aztec": "aztec",
    "pdf417": "pdf417",
    "code128": "code128",
    "code39": "code39",
    "code93": "code93",
    "codabar": "codabar",
    "ean13": "ean_13",
    "ean8": "ean_8",
    "upca": "upc_a",
    "upce": "upc_e",
    "itf": "itf",
    "maxicode": "maxi_code",
    "databar": "data_bar",
    "databarexpanded": "data_bar_expanded",
}


def _normalize_plaintext_format(fmt: str) -> str:
    """Normalize plain-text format name (ZXingReader 2.2.x) to internal form."""
    key = fmt.lower().replace("-", "").replace("_", "").replace(" ", "")
    return _PLAINTEXT_FORMAT_MAP.get(key, _normalize_format(fmt))


def parse_zxing_plaintext_output(stdout: str) -> list[QRResult]:
    """Parse ZXingReader 2.2.x plain key:value output into a list of results.

    The 2.2.x format emits one block per barcode with lines like:
        Text:       "Hello QR"
        Format:     QRCode
        Position:   40x40 250x40 250x250 40x250
        IsMirrored: false
        EC Level:   M
    Blocks are separated by blank lines (or start of next Text: line).
    """
    results: list[QRResult] = []
    current: dict[str, str] = {}

    def _flush(rec: dict[str, str]) -> None:
        fmt_raw = rec.get("Format", "")
        fmt = _normalize_plaintext_format(fmt_raw)
        if not fmt:
            return
        text_raw = rec.get("Text", "")
        # Strip surrounding quotes if present (ZXingReader wraps text in "...")
        if text_raw.startswith('"') and text_raw.endswith('"'):
            text_raw = text_raw[1:-1]
        is_mirrored = rec.get("IsMirrored", "false").strip().lower() == "true"
        results.append(
            QRResult(
                format=fmt,
                value=text_raw,
                position=rec.get("Position"),
                error_correction_level=rec.get("EC Level"),
                is_mirrored=is_mirrored,
                raw_bytes_hex=rec.get("Bytes"),
            )
        )

    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                _flush(current)
                current = {}
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            current[key.strip()] = val.strip()

    if current:
        _flush(current)

    return results


DEFAULT_FORMATS = "qr_code,micro_qr_code,rmqr_code"

# ZXingReader 2.2.x uses different format names for the -format flag
_FORMAT_NAMES_V2 = {
    "qr_code": "QRCode",
    "micro_qr_code": "MicroQRCode",
    "rmqr_code": "rMQRCode",
}


def _translate_formats_for_v2(formats: str) -> str:
    """Convert internal format names to ZXingReader 2.2.x -format values."""
    if not formats:
        return ""
    parts = []
    for f in formats.split(","):
        f = f.strip()
        parts.append(_FORMAT_NAMES_V2.get(f, f))
    return ",".join(parts)


def _default_runner(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
    """Plain subprocess.run — used when no sandbox runner is injected (tests)."""
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout_s, check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def scan_qr(
    png_path: Path,
    *,
    formats: str = DEFAULT_FORMATS,
    timeout_s: int = 10,
    argv_runner=None,
) -> list[QRResult]:
    """Run ZXingReader against a PNG and return the parsed QR results.

    `argv_runner` is a callable `(argv, timeout_s) -> (exit, stdout, stderr)`.
    Defaults to plain `subprocess.run`. The Converter injects a sandbox-
    backed runner so ZXingReader executes under the same namespace /
    seccomp / AppArmor as soffice.

    Supports both ZXingReader >= 2.3 (-json mode) and 2.2.x (plain text mode).

    Raises `ScanError` on runner failure (non-zero exit, timeout,
    missing binary). The Converter catches this and turns it into a
    non-fatal per-page `qr_skipped="error"` entry.
    """
    runner = argv_runner or _default_runner
    bin_path = _zxing_binary()
    use_json = _zxing_supports_json(bin_path, runner=runner)
    if use_json:
        argv = [
            bin_path,
            "-json",
            "-fast",
            "-norotate",
            "-noscale",
            "-formats", formats,
            str(png_path),
        ]
    else:
        # ZXingReader 2.2.x: uses -format (singular) with PascalCase names
        v2_formats = _translate_formats_for_v2(formats)
        argv = [bin_path, "-fast", "-norotate", "-noscale"]
        if v2_formats:
            argv += ["-format", v2_formats]
        argv.append(str(png_path))
    try:
        exit_code, stdout, stderr = runner(argv, timeout_s)
    except subprocess.TimeoutExpired:
        raise ScanError(f"ZXingReader timeout after {timeout_s}s on {png_path.name}")
    except FileNotFoundError:
        raise ScanError("ZXingReader binary not installed — install the zxing-cpp package")
    if exit_code != 0:
        raise ScanError(
            f"ZXingReader exited {exit_code} on {png_path.name}: "
            f"{stderr.strip()[:500]}"
        )
    if use_json:
        return parse_zxing_output(stdout)
    # 2.2.x: "No barcode found" on stdout means empty result
    if "No barcode found" in stdout:
        return []
    return parse_zxing_plaintext_output(stdout)
