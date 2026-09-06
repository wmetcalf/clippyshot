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

import base64
import json
from collections import OrderedDict
import os
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

# How many (lang, psm) tesseract APIs one helper keeps loaded. Each holds a
# language model; four covers realistic per-job variation without unbounded growth.
MAX_CACHED_APIS = 4

# Largest page image sent over the pipe. The helper may be SANDBOXED, in which case
# it has no access to the job filesystem at all and the image travels as bytes -- so
# this is the only bound on what one request can cost in memory. A scan PNG is the
# downscaled copy `select_scan_image` picks, orders of magnitude below this; anything
# larger is refused and the caller cold-falls-back to the CLI, which reads the file
# directly.
MAX_PNG_BYTES = 32 * 1024 * 1024


def sandboxed_popen(sandbox, *, prefix: str | None = None) -> Callable[..., subprocess.Popen]:
    """A ``popen`` for :class:`WarmOCR` that starts the helper INSIDE ``sandbox``.

    The warm helper parses untrusted page images, which is exactly what the inner
    boundary exists for -- but the cold path sandboxes each scanner CALL, mounting that
    page's directory, and a persistent helper's mounts are fixed at spawn, before any
    page exists. That is why the warm tier used to be declined outright under
    nsjail/bwrap (issue #35).

    It works because the image travels as BYTES over the pipe, so the helper needs no
    access to the job filesystem at all -- only to its own interpreter. That is the one
    mount here: ``sys.prefix``, and only when it is not already inside the system
    directories the backend mounts anyway.

    The soffice AppArmor profile is deliberately NOT attached: it is written for
    soffice, and this is a different binary. Namespaces, uid, seccomp and the memory
    and file-size rlimits all still apply.
    """
    from clippyshot.limits import Limits
    from clippyshot.sandbox.base import Mount, SandboxRequest

    import clippyshot

    system = (Path("/usr"), Path("/etc"))

    def _needs_mount(path: Path) -> bool:
        return not any(path == d or path.is_relative_to(d) for d in system)

    root = Path(prefix or sys.prefix).resolve()
    mounts = []
    if _needs_mount(root):
        mounts.append(Mount(root, root, read_only=True))

    # The package's own location, which is NOT always inside sys.prefix: an installed
    # deployment puts it in the venv (covered by the mount above), but a source
    # checkout runs it from a working tree, and a helper that cannot import
    # `clippyshot` never reaches its ready line -- the caller would see only a timeout.
    pkg_root = Path(clippyshot.__file__).resolve().parent.parent
    env: dict[str, str] = {}
    if _needs_mount(pkg_root) and not pkg_root.is_relative_to(root):
        mounts.append(Mount(pkg_root, pkg_root, read_only=True))
        env["PYTHONPATH"] = str(pkg_root)

    def _popen(argv, **kwargs) -> subprocess.Popen:
        # `Limits.from_env()`, not `Limits()`: a deployment that sets CLIPPYSHOT_MEM is
        # capping what an untrusted image parser may allocate, and the cold scanner path
        # honours it. A fresh default here would hand the LONG-LIVED parser the 8 GiB
        # default instead -- the one process where the cap matters most (codex).
        request = SandboxRequest(
            argv=list(argv),
            ro_mounts=mounts,
            rw_mounts=[],
            limits=Limits.from_env(),
            env=dict(env),
            attach_apparmor=False,
        )
        return sandbox.spawn(request, **kwargs)

    return _popen


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
        deadline: float | None = None,
    ) -> OCRResult:
        """OCR one page via the warm helper. Serialised over the single pipe and
        bounded by ``timeout_s``. Raises ``OCRError`` on any failure (caller
        falls back to the cold CLI path).

        ``deadline`` is an absolute ``time.monotonic()`` instant for the job's
        remaining OCR budget. Pass it rather than relying on ``timeout_s`` alone
        whenever several pages share one budget: every caller queues here on one
        lock, and a DURATION computed before the wait is still the full duration
        when the lock is finally acquired -- so a 60s job budget stretches across
        as many pages as are queued. An absolute instant does not decay while
        waiting, which is the whole point of passing one.
        """
        validate_lang(lang)
        with self._lock:
            if deadline is not None:
                # Priced HERE, after the wait, from an instant fixed before it.
                timeout_s = min(timeout_s, deadline - time.monotonic())
                if timeout_s <= 0:
                    raise OCRError("warm OCR budget exhausted while queued")
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None or proc.stdout is None:
                raise OCRError("warm OCR helper not ready")
            # The image travels as BYTES, not as a path. A sandboxed helper cannot
            # read the job's filesystem -- and must not be able to: the whole point of
            # the inner boundary is that the image parser sees nothing but the image.
            # It also sidesteps the fact that a persistent helper's mounts are fixed at
            # spawn, before any page exists (issue #35).
            # Read AT MOST the cap plus one byte. Reading the file whole and checking
            # its length afterwards allocates the very thing the cap exists to bound --
            # the limit would be documentation, not a limit (codex). One extra byte is
            # what distinguishes "exactly at the cap" from "over it", without a stat
            # whose answer could change before the read.
            try:
                with png.open("rb") as fh:
                    data = fh.read(MAX_PNG_BYTES + 1)
            except OSError as e:
                raise OCRError(f"warm OCR cannot read {png}: {e}") from e
            if len(data) > MAX_PNG_BYTES:
                raise OCRError(f"warm OCR page is over the {MAX_PNG_BYTES} byte limit")
            try:
                proc.stdin.write(
                    json.dumps(
                        {
                            "png_b64": base64.b64encode(data).decode("ascii"),
                            "lang": lang,
                            "psm": psm,
                            "timeout_s": timeout_s,
                        }
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
        except Exception:  # noqa: BLE001 - best-effort; SIGKILL is the real teardown
            pass
        # terminate()/wait() can raise (ProcessLookupError/ChildProcessError) if the
        # process was already reaped (e.g. atexit racing a timeout-kill). Stay quiet —
        # teardown must never raise.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:  # noqa: BLE001
            pass


def _build_api_cache(make_api, tessdata):
    """A BOUNDED, least-recently-used `(lang, psm) -> API` cache.

    `lang` and `psm` are job-controlled through the fleet's parameter allowlist
    and the image installs `tesseract-ocr-all`, so an unbounded cache let a
    long-lived warm slot accumulate one loaded model per distinct pair until it
    exhausted the slot's memory. Evicted APIs are End()ed so tesseract frees the
    model rather than leaving it to the GC.

    Takes its constructor so the policy can be tested without tesserocr.
    """
    apis: OrderedDict = OrderedDict()

    def api_for(lang: str, psm: int):
        key = (lang, psm)
        if key in apis:
            apis.move_to_end(key)
            return apis[key]
        kw = {"lang": lang, "psm": psm}
        if tessdata is not None:
            kw["path"] = tessdata
        api = make_api(**kw)
        # Match the CLI path's --dpi 150 (DEFAULT_DPI in clippyshot.ocr).
        api.SetVariable("user_defined_dpi", "150")
        apis[key] = api
        while len(apis) > MAX_CACHED_APIS:
            _, evicted = apis.popitem(last=False)
            try:
                evicted.End()
            except Exception:  # noqa: BLE001 - eviction must never fail a job
                pass
        return apis[key]

    return api_for


def _serve() -> None:  # pragma: no cover - runs in the helper subprocess
    """Helper-subprocess loop: load tesserocr once per (lang, psm), then serve
    one page per stdin request. A per-request failure is non-fatal (returns an
    ``error`` so the caller cold-falls-back). A hang is handled by the client
    SIGKILLing this process — tesseract C calls aren't interruptible here."""
    import glob
    import tempfile

    from tesserocr import PyTessBaseAPI  # PyTessBaseAPI takes psm as a plain int

    def _tessdata_dir() -> str | None:
        # `docker export` (FC/gVisor warm-guest rootfs) DROPS the image's
        # TESSDATA_PREFIX ENV, so tesserocr can't find the models and init fails.
        # Honor a valid env; otherwise discover the system tessdata dir and pass it
        # explicitly — keeps the warm helper working in export-built guest rootfs.
        env = os.environ.get("TESSDATA_PREFIX")
        if env and glob.glob(os.path.join(env, "*.traineddata")):
            return env
        for cand in sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata")) + [
            "/usr/share/tessdata",
            "/usr/local/share/tessdata",
        ]:
            if glob.glob(os.path.join(cand, "*.traineddata")):
                return cand
        return None  # let tesserocr use its default (errors if no models)

    tessdata = _tessdata_dir()
    api_for = _build_api_cache(PyTessBaseAPI, tessdata)

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
            # The page arrives as bytes and is written to a private temp file -- inside
            # a sandboxed helper that is the tmpfs, reachable by nothing else. Handing
            # tesseract a FILE keeps this identical to the cold CLI path rather than
            # introducing a second image-decoding route.
            payload = base64.b64decode(req["png_b64"], validate=True)
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            try:
                tmp.write(payload)
                tmp.close()
                api.SetImageFile(tmp.name)
                text = (api.GetUTF8Text() or "").rstrip("\n")
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
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
