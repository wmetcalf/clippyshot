"""ClippyShot blastbox Engine implementation.

Wraps the ClippyShot conversion pipeline as a blastbox ``Engine``, mapping
the per-page PNG output + metadata dict to the blastbox contract types.

Design note — engine-typed node subclass
-----------------------------------------
``ClippyShotPage`` is registered via ``register_node_type`` so it can be used
as a typed payload node.  However, pydantic's discriminated-union rebuild only
updates the *top-level* ``Node`` alias; nested ``children`` fields on existing
models (``EmbeddedResource.children``, ``Page.children``) keep the pre-rebuild
concrete annotation because their forward-ref was already resolved at class
creation time.  This means a ``ClippyShotPage`` that appears as a *child* of
an ``EmbeddedResource`` cannot be round-tripped through ``envelope_from_json``
on the host.

The safe strategy: use the framework's plain ``Page`` node for the image slot
and attach ClippyShot-specific data (QR / OCR) as ``Record`` children of that
``Page``.  ``ClippyShotPage`` is retained as an exported symbol for callers who
build in-process typed trees (e.g. tests that don't go through
``seal_envelope`` / JSON round-trip).

Usage::

    from clippyshot.engine import ClippyShotEngine
    engine = ClippyShotEngine()
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from blastbox.contract import (
    ArtifactRef,
    DeclaredArtifact,
    Detection,
    Dimensions,
    EmbeddedResource,
    Hash,
    Page,
    Record,
    Warning,
    register_node_type,
)
from blastbox.limits import Limits as BlastboxLimits
from blastbox.worker.engine import DetonationResult

if TYPE_CHECKING:
    from clippyshot.libreoffice.uno import WarmConverter

# ─── ClippyShotPage: exported typed node (in-process use) ───────────────────
# Registered so callers can walk typed in-process trees.  NOT used as a child
# of EmbeddedResource in the wire format (pydantic rebuild limitation — see
# module docstring).


@register_node_type
class ClippyShotPage(Page):
    """A rendered document page produced by ClippyShot (in-process typed node).

    Extends ``Page`` with ClippyShot scanner payloads.  Usable in typed
    in-process trees; not used as the wire-format child node (see engine.py
    module docstring).
    """

    type: Literal["clippyshot_page"] = Field(  # type: ignore[assignment]
        default="clippyshot_page", alias="_type"
    )
    qr: list[dict] = Field(default_factory=list)
    qr_skipped: str | None = None
    ocr: dict = Field(default_factory=dict)


# ─── helpers ────────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"\A[0-9a-fA-F]+\Z")
_HASH_HEXLEN: dict[str, int | None] = {
    "sha256": 64,
    "phash": 16,
    "colorhash": None,  # any positive hex length (14 for binbits=4)
}


def _safe_hash(algo: str, value: str | None) -> Hash | None:
    """Build a Hash, returning None if the value is missing or violates length rules."""
    if not value or not isinstance(value, str):
        return None
    if not _HEX_RE.match(value):
        return None
    expected_len = _HASH_HEXLEN.get(algo)
    if expected_len is not None and len(value) != expected_len:
        return None
    try:
        return Hash(algo=algo, value=value)  # type: ignore[arg-type]
    except Exception:
        return None


def _clamp_confidence(v: float | None) -> float:
    """Clamp confidence to [0.0, 1.0], returning 0.0 on None."""
    if v is None:
        return 0.0
    return max(0.0, min(1.0, float(v)))


def _page_id(index: int) -> str:
    return f"p{index}"


def _trimmed_id(index: int) -> str:
    return f"p{index}-trimmed"


def _focused_id(index: int) -> str:
    return f"p{index}-focused"


def _build_converter(uno_server: WarmConverter | None = None):
    """Build a ClippyShot Converter the same way ``worker._build_converter()`` does.

    ``uno_server`` (the warm tier) is threaded into the LibreOffice runner so the
    soffice→PDF step goes through unoconvert when a server is ready; None keeps the
    cold path."""
    from clippyshot.converter import Converter
    from clippyshot.detector import Detector
    from clippyshot.libreoffice.runner import LibreOfficeRunner
    from clippyshot.rasterizer import build_rasterizer
    from clippyshot.sandbox.detect import select_sandbox
    from clippyshot.selftest import (
        detect_runtime_apparmor_profile,
        detect_soffice_apparmor_profile,
    )

    sandbox = select_sandbox()
    return Converter(
        detector=Detector(),
        runner=LibreOfficeRunner(sandbox=sandbox, uno_server=uno_server),
        rasterizer=build_rasterizer(sandbox),
        sandbox_backend=sandbox.name,
        sandbox=sandbox,
        runtime_apparmor_profile=detect_runtime_apparmor_profile(),
        soffice_apparmor_profile=detect_soffice_apparmor_profile(sandbox),
        seccomp=getattr(sandbox, "seccomp_source", "none"),
    )


def _scanner_record(page_rec: dict) -> Record:
    """Build a Record summarising QR and OCR scanner results for a page.

    This is a ``Record`` child of the ``Page`` node so that scanner data
    survives the JSON → Envelope round-trip through the standard types.
    """
    qr_list = page_rec.get("qr") or []
    qr_skipped = page_rec.get("qr_skipped")
    ocr_obj = page_rec.get("ocr") or {}

    return Record(
        fields={
            "scanner": "clippyshot",
            "qr_count": len(qr_list),
            "qr_skipped": qr_skipped,
            "ocr_text": (ocr_obj.get("text") or "")[:10000],
            "ocr_char_count": ocr_obj.get("char_count", 0),
            "ocr_skipped": ocr_obj.get("skipped"),
        }
    )


# ─── Warm-tier priming ──────────────────────────────────────────────────────

# Minimal flat-ODF (single-XML) documents — used to warm the Impress/Draw paths without
# shipping binary fixtures. A flat presentation/drawing imports + exports through the same
# LibreOffice app + PDF-export filter a real .pptx/.odp / .odg/.vsdx does, minus only the
# binary-OOXML import code (a fraction of the warmup). Validated to convert under LO 25.8/runsc.
_PRIME_FODP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" office:version="1.3" '
    'office:mimetype="application/vnd.oasis.opendocument.presentation">'
    '<office:body><office:presentation><draw:page draw:name="p1"/>'
    "</office:presentation></office:body></office:document>\n"
).encode()
_PRIME_FODG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" office:version="1.3" '
    'office:mimetype="application/vnd.oasis.opendocument.graphics">'
    '<office:body><office:drawing><draw:page draw:name="p1"/>'
    "</office:drawing></office:body></office:document>\n"
).encode()

# Throwaway documents converted during warmup() to load LibreOffice's document framework
# + PDF-export filters BEFORE the warm snapshot is checkpointed. ONE entry per export-filter
# family (writer/calc/impress/draw) — because in the disposable one-job-per-restore model every
# slot is restored from the SAME snapshot, so the snapshot must have ALL families warm or e.g.
# an .xlsx slot pays the Calc warmup the docx prime never covered. The shared framework cost
# (~3.5s) is paid once by the first (txt) prime; each later family prime is sub-second.
# (filename, bytes, detection-label → pdf_filter_for_label family).
_WARM_PRIME_DOCS: tuple[tuple[str, bytes, str], ...] = (
    ("clippyshot-prime.txt", b"clippyshot warm prime\n", "txt"),  # writer_pdf_Export
    ("clippyshot-prime.csv", b"a,b,c\n1,2,3\n", "csv"),  # calc_pdf_Export
    ("clippyshot-prime.fodp", _PRIME_FODP, "fodp"),  # impress_pdf_Export
    ("clippyshot-prime.fodg", _PRIME_FODG, "fodg"),  # draw_pdf_Export
)


def _prime_warm_server(server: "WarmConverter") -> None:
    """Run throwaway conversions against a freshly-started warm server so its filters are
    loaded before the snapshot is taken. Best-effort — never raises (priming is an
    optimization; a failure just means the first real convert pays the warmup once)."""
    import logging
    import tempfile

    log = logging.getLogger("clippyshot.engine")
    with tempfile.TemporaryDirectory(prefix="clippyshot-prime-") as td:
        tdir = Path(td)
        for name, data, label in _WARM_PRIME_DOCS:
            src = tdir / name
            src.write_bytes(data)
            dst = tdir / (name + ".pdf")
            try:
                server.convert(src, dst, label)
            except Exception as exc:  # noqa: BLE001
                log.info("warm prime convert (%s) skipped: %s", label, exc)


# ─── Engine ─────────────────────────────────────────────────────────────────


class ClippyShotEngine:
    """Blastbox Engine wrapping the ClippyShot LibreOffice pipeline.

    ``detonate()`` calls ``Converter.convert()`` and maps the result to the
    blastbox contract types.  The converter is built lazily on first call.
    """

    name: str = "clippyshot"
    formats: frozenset[str] = frozenset({"*"})  # handles all office formats

    def __init__(self) -> None:
        self._converter = None  # lazy
        self._uno_server: WarmConverter | None = None  # set by warmup() in the warm tier

    def warmup(self) -> None:
        """Pre-pay LibreOffice startup for the warm tier (blastbox warm-pool seam).

        With ``CLIPPYSHOT_WARM_UNO=1`` (the FC snapshot / warm-pool tier), ensure a
        persistent warm soffice is listening *before* any input arrives — adopting one
        the rootfs/snapshot already started, or spawning one. Best-effort: a failure
        here leaves ``_uno_server`` None and ``detonate()`` converts via the cold
        soffice path, so a warm hiccup never fails the slot. Off by default → no
        behaviour change for the docker/sandbox tier.

        Transport (``CLIPPYSHOT_WARM_UNO_TRANSPORT``): ``socket`` (default) uses the TCP
        ``unoserver`` (the FC tier); ``pipe`` uses ``soffice --accept=pipe`` (an AF_UNIX
        socket — required by the gVisor C/R tier, whose worker seccomp policy permits only
        AF_UNIX sockets so the TCP unoserver can't bind, and whose UDS acceptor survives
        checkpoint/restore via the accept-retry LD_PRELOAD shim)."""
        if os.environ.get("CLIPPYSHOT_WARM_UNO", "").lower() not in ("1", "true", "yes"):
            return
        import atexit
        import logging

        from clippyshot.libreoffice.profile import HardenedProfile

        # SECURITY: the warm soffice/unoserver MUST boot with the same hardened LibreOffice
        # profile the cold path writes (MacroSecurityLevel=3, DisableMacrosExecution=true, no
        # Basic, no Java, no remote) — otherwise the warm tier parses untrusted documents with
        # LibreOffice's permissive defaults, defeating the control that lets ClippyShot ACCEPT
        # macro-enabled formats. Written here, before the snapshot is taken and before any
        # untrusted input exists, so the lockdown is baked into the warm process captured in the
        # FC/gVisor snapshot at zero per-job cost.
        profile_dir = Path(
            os.environ.get("CLIPPYSHOT_WARM_PROFILE_DIR", "/tmp/.clippyshot-warm-profile")
        )
        try:
            HardenedProfile(profile_dir).write()
            user_installation: str | None = HardenedProfile(profile_dir).url()
        except OSError as exc:
            # Fail CLOSED for a security control: if the hardened profile can't be written, do
            # NOT start an unhardened warm server — leave _uno_server None so detonate() uses the
            # cold path (which writes its own hardened profile per job).
            logging.getLogger("clippyshot.engine").warning(
                "warm-UNO: could not write hardened profile (%s); staying on cold path", exc
            )
            return

        transport = os.environ.get("CLIPPYSHOT_WARM_UNO_TRANSPORT", "socket").strip().lower()
        server: WarmConverter
        if transport == "pipe":
            from clippyshot.libreoffice.uno_pipe import SofficePipeServer

            server = SofficePipeServer(user_installation=user_installation)
        else:
            from clippyshot.libreoffice.uno import UnoServer

            server = UnoServer(user_installation=user_installation)
        try:
            server.start()
        except Exception as exc:
            # Non-fatal: detonate() falls back to cold. Log so warm-tier
            # misconfiguration (missing unoserver/soffice, transport mismatch) is
            # diagnosable.
            logging.getLogger("clippyshot.engine").warning(
                "warm-UNO warmup failed (transport=%s); falling back to cold conversion: %s",
                transport,
                exc,
            )
            return
        # Reap a spawned server if the interpreter exits before reap (adopted
        # servers we don't own are a no-op on stop()).
        atexit.register(server.stop)
        # Prime the conversion path BEFORE the warm snapshot is taken. A freshly-started
        # soffice/unoserver is *listening* but its document framework + import/export filters
        # are unloaded, so the FIRST real conversion pays a ~3-4s warmup (measured 4.65s vs
        # 0.7s steady on gVisor C/R). In the disposable one-job-per-restore model that first
        # convert is the ONLY convert, so without priming the warm tier is barely faster than
        # cold. Running a throwaway conversion here — captured warm in the FC/gVisor snapshot —
        # makes the first post-restore conversion steady-state. Best-effort + opt-out
        # (CLIPPYSHOT_WARM_PRIME=0): a priming failure must never fail the slot.
        if os.environ.get("CLIPPYSHOT_WARM_PRIME", "1").lower() in ("1", "true", "yes"):
            _prime_warm_server(server)
        self._uno_server = server

    def _get_converter(self):
        if self._converter is None:
            self._converter = _build_converter(uno_server=self._uno_server)
        return self._converter

    def detonate(
        self,
        input: Path,
        outdir: Path,
        limits: BlastboxLimits,
    ) -> DetonationResult:
        """Run the ClippyShot pipeline and return a typed ``DetonationResult``.

        Per-page PNGs are written to ``outdir`` by the converter.  This method
        maps the resulting metadata dict to the blastbox contract:

        - Each page → ``Page`` node with ``hashes`` + a ``Record`` child for
          scanner data (QR / OCR).  ``Page`` is used instead of
          ``ClippyShotPage`` here so the typed tree survives the worker
          ``seal_envelope`` → host ``envelope_from_json`` JSON round-trip.
        - Trimmed / focused derivative PNGs → additional ``DeclaredArtifact``
          entries (``id: p{n}-trimmed`` / ``p{n}-focused``).
        - ``document.pdf`` → artifact with ``kind="pdf"``.
        - The root payload is an ``EmbeddedResource`` at path ``"/"`` whose
          children are the ``Page`` nodes.
        """
        from clippyshot.converter import ConvertOptions
        from clippyshot.limits import Limits as CSLimits

        # Map blastbox Limits.timeout_s → ClippyShot Limits (valid range: [1, 600]).
        # Funnel through from_env() so the server path honours every CLIPPYSHOT_* tunable
        # (DPI, MAX_PAGES, MAX_WIDTH/HEIGHT, SKIP_BLANKS, RSS/tmpfs caps) — not just timeout.
        # This is now the ONLY limits-construction point for the server (the bespoke api.py/
        # worker.py were removed when ClippyShot moved onto blastbox.host), so without it the
        # per-document page-count + pixel caps were silently unenforced. The blastbox timeout
        # wins last via the override (preserving the [1, 600] clamp).
        cs_timeout = max(1, min(600, limits.timeout_s))
        cs_limits = CSLimits.from_env(timeout_s=cs_timeout)

        # Per-engine scanner args from the CLIPPYSHOT_* env namespace. The blastbox
        # dispatcher forwards job.params → worker extra_env for keys matching
        # ^[A-Z][A-Z0-9_]*$ (CLIPPYSHOT_* is allowed), so the UI's QR/OCR toggles
        # arrive here as env vars. Defaults preserve the framework-proof behaviour:
        # QR on, OCR off. Values are validated/clamped (untrusted client params).
        def _flag(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

        ocr_lang = (os.environ.get("CLIPPYSHOT_OCR_LANG", "") or "").strip() or "eng+Latin"
        if not re.fullmatch(r"[A-Za-z0-9_+\-]+", ocr_lang):
            ocr_lang = "eng+Latin"
        try:
            ocr_psm = int(os.environ.get("CLIPPYSHOT_OCR_PSM", "3"))
        except (TypeError, ValueError):
            ocr_psm = 3
        ocr_psm = min(13, max(0, ocr_psm))

        cs_opts = ConvertOptions(
            limits=cs_limits,
            qr_enabled=_flag("CLIPPYSHOT_QR", True),
            ocr_enabled=_flag("CLIPPYSHOT_OCR", False),
            ocr_all=_flag("CLIPPYSHOT_OCR_ALL", False),
            ocr_lang=ocr_lang,
            ocr_psm=ocr_psm,
        )

        from clippyshot.errors import DetectionError

        converter = self._get_converter()
        try:
            result = converter.convert(input, outdir, cs_opts)
        except DetectionError as exc:
            # A detector rejection (oversized / unsupported / structural-sanity fail) is a
            # legitimate verdict, not a pipeline error. Surface it as status="rejected" — the
            # dispatcher keeps such a job DONE (dispatch.py gates only engine_error) — instead
            # of letting it reach the harness as a generic engine_error/FAILED. Restores the
            # documented "input rejected" outcome for the server path.
            reason = str(exc)[:2000]
            return DetonationResult(
                payload=EmbeddedResource(
                    embedded_path="/",
                    content_type="application/octet-stream",
                    depth=0,
                    metadata=Record(fields={"label": "rejected", "reason": reason}),
                    children=[],
                ),
                artifacts=[],
                detected=Detection(
                    label="unknown",
                    mime="application/octet-stream",
                    confidence=0.0,
                    source="clippyshot",
                ),
                warnings=[Warning(code="rejected", message=reason)],
                status="rejected",
            )
        meta = result.metadata

        # ── Detection ───────────────────────────────────────────────────────
        inp = meta.get("input", {})
        det = inp.get("detected", {})
        detected = Detection(
            label=det.get("label") or "unknown",
            mime=det.get("mime") or "application/octet-stream",
            confidence=_clamp_confidence(det.get("confidence")),
            source="clippyshot",
        )

        # ── Per-page nodes + artifacts ───────────────────────────────────────
        page_nodes: list[Page] = []
        artifacts: list[DeclaredArtifact] = []

        for page_rec in meta.get("pages", []):
            idx: int = page_rec["index"]
            page_file: str = page_rec["file"]
            page_id = _page_id(idx)

            # Hashes — only include those that pass the contract validators.
            hashes: list[Hash] = []
            for algo in ("phash", "colorhash", "sha256"):
                h = _safe_hash(algo, page_rec.get(algo))
                if h is not None:
                    hashes.append(h)

            # Dimensions from mediabox (millimetres).
            dims = Dimensions(
                width=float(page_rec["width_mm"]),
                height=float(page_rec["height_mm"]),
                unit="mm",
            )

            # Primary PNG artifact.
            artifacts.append(
                DeclaredArtifact(id=page_id, path=page_file, kind="image")
            )

            # Trimmed derivative (if produced and exists on disk).
            trimmed_info = page_rec.get("trimmed")
            if trimmed_info and isinstance(trimmed_info, dict):
                tf = trimmed_info.get("file")
                if tf and (outdir / tf).is_file():
                    tid = _trimmed_id(idx)
                    artifacts.append(DeclaredArtifact(id=tid, path=tf, kind="image"))

            # Focused derivative (spreadsheets only, if produced).
            focused_info = page_rec.get("focused")
            if focused_info and isinstance(focused_info, dict):
                ff = focused_info.get("file")
                if ff and (outdir / ff).is_file():
                    fid = _focused_id(idx)
                    artifacts.append(DeclaredArtifact(id=fid, path=ff, kind="image"))

            # QR / OCR scanner data as a Record child so it survives JSON round-trip.
            scanner_record = _scanner_record(page_rec)

            # Use the standard Page node (not ClippyShotPage) so that
            # EmbeddedResource.children can deserialize this type on the host.
            page_node = Page(
                index=idx,
                dims=dims,
                image=ArtifactRef(id=page_id),
                hashes=hashes,
                children=[scanner_record],
            )
            page_nodes.append(page_node)

        # ── document.pdf artifact (if present) ─────────────────────────────
        pdf_path = outdir / "document.pdf"
        if pdf_path.is_file():
            artifacts.append(
                DeclaredArtifact(id="document-pdf", path="document.pdf", kind="pdf")
            )

        # ── Payload root ─────────────────────────────────────────────────────
        render = meta.get("render", {})
        # Embed the FULL clippyshot metadata (legacy metadata.json schema) as a
        # single JSON string so the host UI can render the complete detail view:
        # detection extras (magika_label/mime, libmagic_mime, agreed_with_extension),
        # render stats (dpi, blank_pages, duration_ms timings, sheet inventory) and
        # per-page derivative stats (trimmed + focused: removed_percent,
        # background_color, dimensions). The typed envelope only carries a thin
        # slice of this; the JSON string carries the rest losslessly. A scalar
        # string adds no node-count/depth pressure (the depth guard is
        # string-aware). Fail-open: a serialization hiccup must never fail the job.
        try:
            metadata_json = json.dumps(meta, default=str)
        except (TypeError, ValueError):
            metadata_json = ""
        payload = EmbeddedResource(
            embedded_path="/",
            content_type=det.get("mime") or "application/octet-stream",
            depth=0,
            metadata=Record(
                fields={
                    "label": det.get("label") or "unknown",
                    "page_count_total": render.get("page_count_total", 0),
                    "page_count_rendered": render.get("page_count_rendered", 0),
                    "truncated": bool(render.get("truncated", False)),
                    "clippyshot_version": meta.get("clippyshot_version", ""),
                    "clippyshot_metadata": metadata_json,
                }
            ),
            children=list(page_nodes),
        )

        # ── Warnings ─────────────────────────────────────────────────────────
        warnings: list[Warning] = []
        for w in meta.get("warnings", []):
            if not isinstance(w, dict):
                continue
            code = str(w.get("code") or "warning")[:64]
            message = str(w.get("message") or "")[:2000]
            if not code:
                code = "warning"
            warnings.append(Warning(code=code, message=message))

        return DetonationResult(
            payload=payload,
            artifacts=artifacts,
            detected=detected,
            warnings=warnings,
            status="ok",
        )
