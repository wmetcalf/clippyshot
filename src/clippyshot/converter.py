"""Conversion pipeline orchestration."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pypdf import PdfReader

from clippyshot._version import __version__
from clippyshot.errors import (
    ClippyShotError,
    ConversionError,
    DetectionError,
    LibreOfficeError,
    RasterizeError,
)
from clippyshot.hasher import hash_png_bytes
from clippyshot.limits import Limits
from clippyshot.observability import (
    JOBS_IN_FLIGHT,
    get_logger,
    record_conversion,
    record_rejection,
)
from clippyshot.detector import MACRO_ENABLED_EXTENSIONS
from clippyshot.rasterizer.base import Rasterizer
from clippyshot.sandbox.base import Mount, Sandbox, SandboxRequest
from clippyshot.types import DetectedType


_log = get_logger("clippyshot.converter")

_PT_PER_INCH = 72.0
_MM_PER_INCH = 25.4
_FOCUSED_DERIVATIVE_LABELS = frozenset({"xlsx", "xlsm", "xls", "xlsb", "ods", "fods", "csv"})




def _mediabox_mm(page) -> tuple[float, float]:
    """Convert a pypdf page's mediabox from points to millimetres."""
    box = page.mediabox
    w_mm = (float(box.width) / _PT_PER_INCH) * _MM_PER_INCH
    h_mm = (float(box.height) / _PT_PER_INCH) * _MM_PER_INCH
    return (w_mm, h_mm)


# PDF content-stream operators that indicate non-text rendering. Path-paint
# operators (f, F, S, B, b, their `*` variants) commit a drawing to the page.
# `Do` invokes an XObject (form or image). `BI` starts an inline image.
# These are matched as whole-word tokens — we search with leading whitespace
# + trailing whitespace/newline to avoid catching substrings of other words.
import re
_NON_TEXT_OPS_RE = re.compile(rb"(?m)(?:^|\s)(?:Do|BI|f|F|S|B|b|f\*|B\*|b\*)(?:$|\s)")


def _page_has_non_text_graphics(page) -> bool:
    """True if the PDF page has raster images, XObject invocations, or path-paint ops.

    Used to decide whether a page is OCR-worthy beyond just raw raster
    images — vector drawings, charts, stamps, and decorative shapes all
    show up as path-paint operators in the content stream, and all can
    carry text-in-image content we want tesseract to see.
    """
    contents = page.get("/Contents")
    if contents is None:
        return False
    try:
        contents = contents.get_object()
    except Exception:
        return False
    # Contents can be a single stream or an array of streams.
    try:
        if hasattr(contents, "get_data"):
            data = contents.get_data()
        else:
            chunks = []
            for entry in contents:
                try:
                    chunks.append(entry.get_object().get_data())
                except Exception:
                    continue
            data = b"\n".join(chunks)
    except Exception:
        return False
    # Check for whole-op matches using a pre-compiled regex to avoid
    # a massive memory allocation from data.split() on gigabyte payloads.
    return bool(_NON_TEXT_OPS_RE.search(data))


def select_scan_image(output_dir: Path, page_record: dict) -> Path | None:
    """Return the best PNG path to feed QR/OCR for a page, or None if missing.

    Preference order: focused > trimmed > original. Derivatives are
    tighter crops with less whitespace and fewer false positives for
    QR, and they speed up tesseract. The page_record shape is the dict
    the Converter builds — see `_process_page` in this module.
    """
    for key in ("focused", "trimmed"):
        deriv = page_record.get(key)
        if deriv and isinstance(deriv, dict):
            candidate = output_dir / deriv.get("file", "")
            if candidate.is_file():
                return candidate
    original = output_dir / page_record.get("file", "")
    return original if original.is_file() else None


def _copy_pdf_to_output(pdf_src: Path, output_dir: Path) -> Path | None:
    """Copy the intermediate PDF to `output_dir/document.pdf`, overwriting.

    Returns the destination path, or None if the source doesn't exist
    (soffice failed earlier in the pipeline).
    """
    import shutil as _sh
    if not pdf_src.is_file():
        return None
    dest = output_dir / "document.pdf"
    _sh.copyfile(pdf_src, dest)
    return dest


def _make_sandbox_argv_runner(sandbox: Sandbox, limits: Limits | None, scan_png_host: Path, sandbox_scan_path: Path):
    """Return an argv_runner callable compatible with scan_qr/run_ocr.

    The runner wraps the scanner's argv in a `SandboxRequest` that
    bind-mounts the PNG's parent dir read-only into the sandbox. The
    scanner's argv may reference any file in that parent dir — the
    runner rewrites host paths to sandbox paths at the directory level,
    so scanners that produce sibling files (e.g. an OCR-downscaled copy
    next to the original PNG) can pass the sibling's host path and have
    it translated correctly.
    """
    from dataclasses import replace
    scan_dir_host = str(scan_png_host.parent)
    scan_dir_sandbox = str(sandbox_scan_path.parent)

    def run(argv, timeout_s):
        req_limits = replace(limits, timeout_s=timeout_s) if limits is not None else Limits(timeout_s=timeout_s)
        # Substitute the parent-dir prefix so any file in the scan dir
        # (original PNG, downscaled OCR copy, etc.) gets translated to
        # its sandbox path without each scanner needing to know the
        # sandbox mount point.
        sandbox_argv = [
            arg.replace(scan_dir_host, scan_dir_sandbox)
            for arg in argv
        ]
        req = SandboxRequest(
            argv=sandbox_argv,
            ro_mounts=[Mount(scan_png_host.parent, sandbox_scan_path.parent, read_only=True)],
            rw_mounts=[],
            limits=req_limits,
            env={},
        )
        result = sandbox.run(req)
        return (
            result.exit_code,
            (result.stdout or b"").decode("utf-8", errors="replace"),
            (result.stderr or b"").decode("utf-8", errors="replace"),
        )
    return run


def _process_page_scanners(
    output_dir: Path,
    page_record: dict,
    *,
    is_blank: bool,
    qr_enabled: bool,
    qr_formats: str,
    qr_timeout_s: int,
    ocr_enabled: bool,
    ocr_lang: str,
    ocr_psm: int,
    ocr_time_left=None,         # callable returning remaining seconds; replaces per-page ocr_timeout_s
    has_images: bool = True,
    ocr_all: bool = False,
    _qr_fn=None,
    _ocr_fn=None,
    qr_runner=None,
    ocr_runner=None,
) -> tuple[list[dict], str | None, dict, list[dict]]:
    """Run QR and OCR for one page, catching errors.

    Returns `(qr_list, qr_skipped, ocr_obj, warnings)`:
    - `qr_list`: list of QR result dicts (may be empty)
    - `qr_skipped`: None if scan ran normally, else a reason string
      (`"blank_page"`, `"disabled"`, `"timeout"`, `"error"`)
    - `ocr_obj`: dict always containing `text`, `char_count`,
      `duration_ms`. If the scan didn't run, also contains
      `skipped` with a reason.
    - `warnings`: structured warning dicts to append to `metadata.warnings`.
      Empty in the happy path.

    `has_images`: the caller's "OCR would add value on this page" signal.
    Typically set when the PDF page has raster images OR no text layer
    (scanned pages). Pure-text pages with a populated text layer are
    already searchable and OCR would duplicate, so the helper skips them
    with reason `"no_images"` when ``ocr_enabled=True`` AND
    ``ocr_all=False`` AND ``has_images=False``.

    `ocr_time_left`: callable returning the remaining OCR budget in seconds
    for the current job. When the budget is exhausted (returns 0.0 or less),
    OCR is skipped with reason `"timeout_budget"`. Per-call tesseract timeout
    floors at 30s so no single page can wedge indefinitely.

    `qr_runner` and `ocr_runner` are sandbox-backed argv runners the
    caller may pass through. Tests leave them None.
    """
    if _qr_fn is None:
        from clippyshot.qr import scan_qr as _qr_fn
    if _ocr_fn is None:
        from clippyshot.ocr import run_ocr as _ocr_fn

    warnings: list[dict] = []
    qr_list: list[dict] = []
    qr_skipped: str | None = None
    ocr_obj: dict = {"text": "", "char_count": 0, "duration_ms": 0}

    # QR branch
    if not qr_enabled:
        qr_skipped = "disabled"
    elif is_blank:
        qr_skipped = "blank_page"
    else:
        scan_png = select_scan_image(output_dir, page_record)
        if scan_png is None:
            qr_skipped = "error"
            warnings.append({
                "code": "qr_scan_error",
                "page": page_record.get("index"),
                "message": "no scannable PNG found for page",
            })
        else:
            try:
                kwargs = {"formats": qr_formats, "timeout_s": qr_timeout_s}
                if qr_runner is not None:
                    kwargs["argv_runner"] = qr_runner
                results = _qr_fn(scan_png, **kwargs)
                qr_list = [
                    {
                        "format": r.format,
                        "value": r.value,
                        "position": r.position,
                        "error_correction_level": r.error_correction_level,
                        "is_mirrored": r.is_mirrored,
                        "raw_bytes_hex": r.raw_bytes_hex,
                    }
                    for r in results
                ]
            except Exception as e:
                qr_skipped = "timeout" if "timeout" in str(e).lower() else "error"
                warnings.append({
                    "code": "qr_scan_error",
                    "page": page_record.get("index"),
                    "message": str(e)[:500],
                })

    # OCR branch
    if not ocr_enabled:
        ocr_obj["skipped"] = "disabled"
    elif is_blank:
        ocr_obj["skipped"] = "blank_page"
    elif not ocr_all and not has_images:
        # Default mode: only OCR pages that have embedded images in the
        # source PDF. Pure-text pages have a PDF text layer already.
        ocr_obj["skipped"] = "no_images"
    else:
        remaining = ocr_time_left() if ocr_time_left is not None else 60.0
        if remaining <= 0:
            ocr_obj["skipped"] = "timeout_budget"
        else:
            scan_png = select_scan_image(output_dir, page_record)
            if scan_png is None:
                ocr_obj["skipped"] = "error"
                warnings.append({
                    "code": "ocr_scan_error",
                    "page": page_record.get("index"),
                    "message": "no scannable PNG found for page",
                })
            else:
                # Per-call timeout floors at 30s so tesseract can always
                # fail cleanly, even when the budget is nearly exhausted.
                per_call_timeout = max(30, int(remaining))
                try:
                    kwargs = {"lang": ocr_lang, "psm": ocr_psm, "timeout_s": per_call_timeout}
                    if ocr_runner is not None:
                        kwargs["argv_runner"] = ocr_runner
                    result = _ocr_fn(scan_png, **kwargs)
                    ocr_obj = {
                        "text": result.text,
                        "char_count": result.char_count,
                        "duration_ms": result.duration_ms,
                    }
                except Exception as e:
                    ocr_obj["skipped"] = "timeout" if "timeout" in str(e).lower() else "error"
                    warnings.append({
                        "code": "ocr_scan_error",
                        "page": page_record.get("index"),
                        "message": str(e)[:500],
                    })

    return qr_list, qr_skipped, ocr_obj, warnings


@dataclass(frozen=True)
class ConvertOptions:
    limits: Limits = field(default_factory=Limits)
    # QR scanning — on by default
    qr_enabled: bool = True
    qr_formats: str = "qr_code,micro_qr_code,rmqr_code"
    qr_timeout_s: int = 10
    # OCR scanning — opt in
    ocr_enabled: bool = False
    ocr_all: bool = False                   # when False, OCR only image-bearing pages
    ocr_lang: str = "eng+Latin"
    ocr_psm: int = 3
    ocr_timeout_s: int = 60


class _DetectorLike(Protocol):
    def detect(self, path: Path, *, max_input_bytes: int | None = None) -> DetectedType: ...


class _RunnerLike(Protocol):
    def convert_to_pdf(self, input_path: Path, output_dir: Path, limits: Limits, label: str) -> Path: ...


@dataclass
class ConversionResult:
    output_dir: Path
    metadata: dict


class Converter:
    """Pipeline: detector → libreoffice → rasterizer → hasher → metadata.

    `Converter.convert()` is reentrant: each call creates its own profile dir,
    its own sandbox invocation, and its own output directory. Concurrent
    conversions are independent at the OS level (separate processes, separate
    namespaces, separate tmpfs mounts).
    """

    def __init__(
        self,
        *,
        detector: _DetectorLike,
        runner: _RunnerLike,
        rasterizer: Rasterizer,
        sandbox_backend: str,
        sandbox: Sandbox | None = None,
        apparmor_profile: str = "unknown",
        runtime_apparmor_profile: str | None = None,
        soffice_apparmor_profile: str | None = None,
        seccomp: str = "none",
    ) -> None:
        self._detector = detector
        self._runner = runner
        self._rasterizer = rasterizer
        self._sandbox = sandbox
        self._sandbox_backend = sandbox_backend
        # Back-compat: if caller only passed the legacy `apparmor_profile`
        # field, it was the runtime's measured profile. Map it into the new
        # split fields so existing tests and callers continue to work.
        self._runtime_apparmor_profile = (
            runtime_apparmor_profile if runtime_apparmor_profile is not None
            else apparmor_profile
        )
        self._soffice_apparmor_profile = (
            soffice_apparmor_profile if soffice_apparmor_profile is not None
            else "clippyshot-soffice"
        )
        # Where the seccomp filter applied to soffice comes from. Values:
        #   "clippyshot-nsjail"   — our KAFEL policy via nsjail --seccomp_policy
        #   "clippyshot-bwrap"    — our libseccomp-built BPF via bwrap --seccomp
        #   "container-runtime"   — inherited from Docker's docker-default /
        #                           Kubernetes RuntimeDefault profile (the
        #                           container IS the sandbox)
        #   "none"                — no filter at all (bare-host bwrap without
        #                           libseccomp bindings, or an unknown setup)
        self._seccomp = seccomp
        # Retained for callers that still read the old field.
        self._apparmor_profile = self._runtime_apparmor_profile

    def convert(
        self,
        input_path: Path,
        output_dir: Path,
        options: ConvertOptions,
    ) -> ConversionResult:
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        log = _log.bind(input=str(input_path))
        log.info("conversion_started", size_bytes=input_path.stat().st_size)

        timings: dict[str, int] = {}
        t_total = time.monotonic()
        JOBS_IN_FLIGHT.inc()
        try:
            # 1. Detect.
            t = time.monotonic()
            try:
                detected = self._detector.detect(
                    input_path, max_input_bytes=options.limits.max_input_bytes
                )
            except DetectionError as e:
                record_rejection(e.reason)
                log.warning("conversion_rejected", reason=e.reason, detail=e.detail)
                raise
            timings["detect"] = int((time.monotonic() - t) * 1000)

            # 2. soffice → PDF. The intermediate PDF contains the full
            # rendered payload of a potentially-hostile document. Keep
            # it in a container-private tmpfs (tempfile.mkdtemp respects
            # TMPDIR / $TMP / /tmp) so it never touches the bind-mounted
            # output dir — a SIGKILL'd worker would otherwise leak the
            # intermediate PDF to the host volume.
            pdf_dir = Path(tempfile.mkdtemp(prefix="clippyshot-pdf-"))
            pdf_path: Path | None = None
            try:
                t = time.monotonic()
                try:
                    pdf_path = self._runner.convert_to_pdf(input_path, pdf_dir, options.limits, detected.label)
                except LibreOfficeError as e:
                    raise ConversionError(f"libreoffice failed: {e}", cause=e) from e
                timings["soffice"] = int((time.monotonic() - t) * 1000)

                # 3. Page count + truncation decision + mediabox dimensions.
                # Open the PDF once and reuse it for both the page count AND
                # the per-page mediabox dimensions. The rasterizer accepts the
                # mediaboxes as a parameter to avoid re-opening the PDF.
                try:
                    reader = PdfReader(str(pdf_path))
                    total_pages = len(reader.pages)
                    pages_to_render = min(total_pages, options.limits.max_pages)
                    page_sizes_mm = [
                        _mediabox_mm(reader.pages[i]) for i in range(pages_to_render)
                    ]
                    # Detect which PDF pages are OCR-worthy. We run OCR on a
                    # page when ANY of:
                    #   (a) the page carries raster images (pypdf reports
                    #       at least one /Image XObject),
                    #   (b) the page contains non-text graphics — vector
                    #       drawings, charts, shapes, stamped labels, or
                    #       form XObjects (detected by scanning the
                    #       content stream for path-paint / Do / BI
                    #       operators), or
                    #   (c) the PDF text layer is empty — typical of
                    #       scanned PDFs, where the only textual content
                    #       lives in the rendered pixels.
                    # The threat-analysis use case needs OCR any time
                    # there's visual content beyond pure text, because
                    # malicious payloads frequently sit in diagrams,
                    # stamps, QR-like shapes, or overlay drawings that
                    # the PDF text layer can't see.
                    # Pure-text pages (populated text layer AND no images
                    # AND no drawings) are skipped because OCR would just
                    # duplicate what's already searchable.
                    page_image_counts: list[int] = []
                    page_has_drawings: list[bool] = []
                    page_text_empty: list[bool] = []
                    for i in range(pages_to_render):
                        page = reader.pages[i]
                        try:
                            page_image_counts.append(len(list(page.images)))
                        except Exception:
                            # Malformed XObject stream. Default to 1
                            # (conservative: we want OCR to run when we
                            # can't confirm the page is purely text).
                            page_image_counts.append(1)
                        try:
                            page_has_drawings.append(_page_has_non_text_graphics(page))
                        except Exception:
                            # Malformed content stream: bias toward OCR.
                            page_has_drawings.append(True)
                        if page_image_counts[-1] > 0 or page_has_drawings[-1]:
                            page_text_empty.append(False)
                        else:
                            try:
                                text = page.extract_text() or ""
                                page_text_empty.append(not text.strip())
                            except Exception:
                                # Text extraction failed — treat as empty so
                                # OCR runs (same conservative bias).
                                page_text_empty.append(True)
                except Exception as e:
                    raise ConversionError(f"could not read PDF: {e}", cause=e) from e
                truncated = total_pages > options.limits.max_pages

                # 4. Rasterize.
                t = time.monotonic()
                try:
                    pages = self._rasterizer.rasterize(
                        pdf_path,
                        output_dir,
                        dpi=options.limits.dpi,
                        max_pages=pages_to_render,
                        page_sizes_mm=page_sizes_mm,
                    )
                except RasterizeError as e:
                    raise ConversionError(f"rasterize failed: {e}", cause=e) from e
                
                # Check rendered dimensions against limits.
                for page in pages:
                    if page.width_px > options.limits.max_width_px or page.height_px > options.limits.max_height_px:
                        raise RasterizeError(
                            f"page {page.index} dimensions {page.width_px}x{page.height_px} "
                            f"exceed limits {options.limits.max_width_px}x{options.limits.max_height_px}"
                        )

                timings["rasterize"] = int((time.monotonic() - t) * 1000)

                # 5. Postprocess each PNG: original hashes, derivatives, derivative hashes.
                # Parallelised — each page is independent, and PIL+numpy release
                # the GIL during decode/DCT/array ops, so threads beat processes
                # here (no pickling, no re-importing modules).
                t_postprocess = time.monotonic()
                from clippyshot.trimmer import focus_content_solid_bg, trim_bottom_solid
                from concurrent.futures import ThreadPoolExecutor
                import os as _os

                _want_focused = detected.label in _FOCUSED_DERIVATIVE_LABELS

                # OCR is bounded by a single per-job wall-clock budget
                # (options.ocr_timeout_s seconds). Once that elapses, the
                # remaining non-blank image-bearing pages get skipped with
                # `skipped="timeout_budget"`. Per-invocation tesseract
                # timeout floors at max(30s, remaining budget) so no single
                # page can wedge, but the total cannot exceed ocr_timeout_s.
                import time as _time
                ocr_budget_deadline = _time.monotonic() + options.ocr_timeout_s

                def _ocr_time_left() -> float:
                    return max(0.0, ocr_budget_deadline - _time.monotonic())

                def _process_page(page_with_flag) -> tuple[dict, bool, dict, list[dict]]:
                    """Return (record, is_blank, stage_timings, warnings)."""
                    page, image_count, has_drawings, text_empty = page_with_flag
                    # OCR-worthy if the page has any non-text visual
                    # content: raster images, vector drawings / stamps,
                    # OR an empty text layer (scanned PDFs).
                    ocr_worthy = image_count > 0 or has_drawings or text_empty
                    stage_t = {
                        "hash_original": 0, "trim": 0, "focus": 0,
                        "hash_derivatives": 0, "qr": 0, "ocr": 0,
                    }
                    png_path = output_dir / page.path

                    t_stage = time.monotonic()
                    png_bytes = png_path.read_bytes()
                    h = hash_png_bytes(png_bytes)
                    stage_t["hash_original"] += int((time.monotonic() - t_stage) * 1000)

                    rec = {
                        "index": page.index,
                        "file": page.path,
                        "width_px": page.width_px,
                        "height_px": page.height_px,
                        "width_mm": page.width_mm,
                        "height_mm": page.height_mm,
                        **h.to_dict(),
                    }
                    rec["image_count"] = image_count
                    if not h.is_blank:
                        t_stage = time.monotonic()
                        trim_info = trim_bottom_solid(png_path)
                        stage_t["trim"] += int((time.monotonic() - t_stage) * 1000)
                        if trim_info:
                            trimmed_path = output_dir / trim_info["file"]
                            t_hash = time.monotonic()
                            trimmed_hashes = hash_png_bytes(trimmed_path.read_bytes())
                            stage_t["hash_derivatives"] += int((time.monotonic() - t_hash) * 1000)
                            trim_info.update(trimmed_hashes.to_dict())
                            rec["trimmed"] = trim_info
                        if _want_focused:
                            t_stage = time.monotonic()
                            focused_info = focus_content_solid_bg(png_path)
                            stage_t["focus"] += int((time.monotonic() - t_stage) * 1000)
                            if focused_info:
                                focused_path = output_dir / focused_info["file"]
                                t_hash = time.monotonic()
                                focused_hashes = hash_png_bytes(focused_path.read_bytes())
                                stage_t["hash_derivatives"] += int((time.monotonic() - t_hash) * 1000)
                                focused_info.update(focused_hashes.to_dict())
                                rec["focused"] = focused_info
                    # Scanners (always evaluated; helper short-circuits internally on blank / disabled).
                    t_qr_ocr = time.monotonic()
                    # Build a sandbox-backed argv runner if we have a sandbox.
                    # Otherwise scan_qr/run_ocr fall back to subprocess.run.
                    qr_runner = ocr_runner = None
                    scanners_needed = options.qr_enabled or options.ocr_enabled
                    if self._sandbox is not None and not h.is_blank and scanners_needed:
                        scan_png_host = select_scan_image(output_dir, rec)
                        if scan_png_host is not None:
                            # Inside the sandbox, mount the PNG's parent at /sandbox/scan.
                            sandbox_scan_path = Path("/sandbox/scan") / scan_png_host.name
                            runner = _make_sandbox_argv_runner(
                                self._sandbox, options.limits,
                                scan_png_host, sandbox_scan_path,
                            )
                            qr_runner = ocr_runner = runner
                    qr_list, qr_skipped, ocr_obj, page_warnings = _process_page_scanners(
                        output_dir, rec,
                        is_blank=h.is_blank,
                        qr_enabled=options.qr_enabled,
                        qr_formats=options.qr_formats,
                        qr_timeout_s=options.qr_timeout_s,
                        ocr_enabled=options.ocr_enabled,
                        ocr_lang=options.ocr_lang,
                        ocr_psm=options.ocr_psm,
                        ocr_time_left=_ocr_time_left,
                        has_images=ocr_worthy,
                        ocr_all=options.ocr_all,
                        qr_runner=qr_runner,
                        ocr_runner=ocr_runner,
                    )
                    qr_or_ocr_ms = int((time.monotonic() - t_qr_ocr) * 1000)
                    stage_t["ocr"] = ocr_obj.get("duration_ms", 0)
                    stage_t["qr"] = max(0, qr_or_ocr_ms - stage_t["ocr"])
                    rec["qr"] = qr_list
                    if qr_skipped is not None:
                        rec["qr_skipped"] = qr_skipped
                    rec["ocr"] = ocr_obj
                    return rec, h.is_blank, stage_t, page_warnings

                max_workers = min(len(pages), (_os.cpu_count() or 2), 8) if pages else 1
                pages_with_flags = list(zip(pages, page_image_counts, page_has_drawings, page_text_empty))
                if max_workers <= 1:
                    results = [_process_page(p) for p in pages_with_flags]
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as ex:
                        # ex.map preserves input order.
                        results = list(ex.map(_process_page, pages_with_flags))

                timings["hash_original"] = sum(r[2]["hash_original"] for r in results)
                timings["trim"] = sum(r[2]["trim"] for r in results)
                timings["focus"] = sum(r[2]["focus"] for r in results)
                timings["hash_derivatives"] = sum(r[2]["hash_derivatives"] for r in results)
                timings["qr"] = sum(r[2]["qr"] for r in results)
                timings["ocr"] = sum(r[2]["ocr"] for r in results)

                all_page_records = [r[0] for r in results]
                blank_indices = [r[0]["index"] for r in results if r[1]]
                # Accumulate structured per-page scanner warnings to merge
                # into metadata.warnings below.
                scanner_warnings: list[dict] = [w for r in results for w in r[3]]

                # Backward-compatible aggregate: full postprocess wall-clock bucket.
                timings["hash"] = int((time.monotonic() - t_postprocess) * 1000)

                # 5c. Filter blanks if requested.
                if options.limits.skip_blanks and blank_indices:
                    page_records = [
                        r for r in all_page_records if r["index"] not in set(blank_indices)
                    ]
                    # Delete the blank PNG files so they don't bloat the output dir.
                    for idx in blank_indices:
                        png = output_dir / f"page-{idx:03d}.png"
                        if png.exists():
                            png.unlink()
                else:
                    page_records = all_page_records

                timings["total"] = int((time.monotonic() - t_total) * 1000)

                # 6. Metadata. Stream the SHA-256 so we don't buffer the
                # entire (up to 100MB) input into RAM just to hash it.
                _h = hashlib.sha256()
                with open(input_path, "rb") as _fh:
                    for _chunk in iter(lambda: _fh.read(1 << 20), b""):
                        _h.update(_chunk)
                input_sha256 = _h.hexdigest()
                warnings = []
                if not detected.agreed_with_extension:
                    warnings.append({
                        "code": "extension_mismatch",
                        "message": "input extension did not agree with detected type",
                    })
                if detected.extension_hint in MACRO_ENABLED_EXTENSIONS:
                    warnings.append({
                        "code": "macro_enabled_format",
                        "message": (
                            f"input extension '.{detected.extension_hint}' declares macro support; "
                            "ClippyShot's hardened LibreOffice profile prevents macro execution"
                        ),
                    })
                warnings.extend(scanner_warnings)

                # Build the security block. When disclose_security_internals is
                # False (the default), omit backend name and AppArmor profile
                # names to reduce deployment fingerprinting surface.
                if options.limits.disclose_security_internals:
                    security_block: dict = {
                        "sandbox": self._sandbox_backend,
                        # Kept for backwards compatibility with consumers that
                        # already read `apparmor_profile`. Now always the
                        # runtime's measured profile (same value as
                        # `runtime_apparmor_profile`).
                        "apparmor_profile": self._runtime_apparmor_profile,
                        "runtime_apparmor_profile": self._runtime_apparmor_profile,
                        "soffice_apparmor_profile": self._soffice_apparmor_profile,
                        "seccomp": self._seccomp,
                        "macro_security_level": 3,
                        "network": "denied",
                        "java": "disabled",
                    }
                else:
                    security_block = {
                        "macro_security_level": 3,
                        "network": "denied",
                        "macros": "disabled",
                        "java": "disabled",
                    }

                metadata = {
                    "clippyshot_version": __version__,
                    "input": {
                        "filename": input_path.name,
                        "size_bytes": input_path.stat().st_size,
                        "sha256": input_sha256,
                        "detected": {
                            "source": detected.source,
                            "label": detected.label,
                            "mime": detected.mime,
                            "confidence": detected.confidence,
                            "extension_hint": detected.extension_hint,
                            "agreed_with_extension": detected.agreed_with_extension,
                            "magika_label": detected.magika_label,
                            "magika_mime": detected.magika_mime,
                            "libmagic_mime": detected.libmagic_mime,
                        },
                    },
                    "render": {
                        "engine": "libreoffice",
                        "rasterizer": self._rasterizer.name,
                        "dpi": options.limits.dpi,
                        "page_count_total": total_pages,
                        "page_count_rendered": len(page_records),
                        "truncated": truncated,
                        "blank_pages_skipped": len(blank_indices) if options.limits.skip_blanks else 0,
                        "blank_pages": blank_indices,
                        "image_page_count": sum(1 for r in results if r[0].get("image_count", 0) > 0),
                        "total_image_count": sum(r[0].get("image_count", 0) for r in results),
                        "scanners": {
                            "qr": {
                                "enabled": options.qr_enabled,
                                "formats": options.qr_formats if options.qr_enabled else None,
                            },
                            "ocr": {
                                "enabled": options.ocr_enabled,
                                "lang": options.ocr_lang if options.ocr_enabled else None,
                                "psm": options.ocr_psm if options.ocr_enabled else None,
                                "all_pages": options.ocr_all,
                            },
                        },
                        "duration_ms": timings,
                    },
                    "security": security_block,
                    "pages": page_records,
                    "warnings": warnings,
                    "errors": [],
                }
                (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
            finally:
                # 7. Copy the intermediate PDF to the output dir as
                # `document.pdf` (so it ships with the result zip and is
                # servable via GET /v1/jobs/{id}/pdf), then clean the
                # tmpfs pdf_dir. Runs on every exit path — if soffice
                # failed earlier, pdf_path is None and the copy is a no-op.
                try:
                    if pdf_path is not None and pdf_path.is_file():
                        _copy_pdf_to_output(pdf_path, output_dir)
                except Exception:
                    _log.exception("pdf_copy_failed")
                shutil.rmtree(pdf_dir, ignore_errors=True)

            record_conversion(
                outcome="success",
                format_=detected.label,
                duration_ms=timings["total"],
                stage_durations=timings,
            )
            log.info("conversion_finished", outcome="success", **timings)
            return ConversionResult(output_dir=output_dir, metadata=metadata)
        except DetectionError:
            raise
        except ClippyShotError as e:
            record_conversion(
                outcome="failure",
                format_="unknown",
                duration_ms=int((time.monotonic() - t_total) * 1000),
                stage_durations=timings,
            )
            log.error("conversion_failed", error=str(e))
            raise
        finally:
            JOBS_IN_FLIGHT.dec()
