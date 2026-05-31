"""ClippyShot detonator Engine implementation.

Wraps the ClippyShot conversion pipeline as a detonator ``Engine``, mapping
the per-page PNG output + metadata dict to the detonator contract types.

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

import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from detonator.contract import (
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
from detonator.contract.nodes import _Node
from detonator.limits import Limits as DetonatorLimits
from detonator.worker.engine import DetonationResult

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


def _build_converter():
    """Build a ClippyShot Converter the same way ``worker._build_converter()`` does."""
    from clippyshot.converter import Converter
    from clippyshot.detector import Detector
    from clippyshot.libreoffice.runner import LibreOfficeRunner
    from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
    from clippyshot.sandbox.detect import select_sandbox
    from clippyshot.selftest import (
        detect_runtime_apparmor_profile,
        detect_soffice_apparmor_profile,
    )

    sandbox = select_sandbox()
    return Converter(
        detector=Detector(),
        runner=LibreOfficeRunner(sandbox=sandbox),
        rasterizer=PdftoppmRasterizer(sandbox=sandbox),
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


# ─── Engine ─────────────────────────────────────────────────────────────────


class ClippyShotEngine:
    """Detonator Engine wrapping the ClippyShot LibreOffice pipeline.

    ``detonate()`` calls ``Converter.convert()`` and maps the result to the
    detonator contract types.  The converter is built lazily on first call.
    """

    name: str = "clippyshot"
    formats: frozenset[str] = frozenset({"*"})  # handles all office formats

    def __init__(self) -> None:
        self._converter = None  # lazy

    def _get_converter(self):
        if self._converter is None:
            self._converter = _build_converter()
        return self._converter

    def detonate(
        self,
        input: Path,
        outdir: Path,
        limits: DetonatorLimits,
    ) -> DetonationResult:
        """Run the ClippyShot pipeline and return a typed ``DetonationResult``.

        Per-page PNGs are written to ``outdir`` by the converter.  This method
        maps the resulting metadata dict to the detonator contract:

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

        # Map detonator Limits.timeout_s → ClippyShot Limits (valid range: [1, 600]).
        cs_timeout = max(1, min(600, limits.timeout_s))
        cs_limits = CSLimits(timeout_s=cs_timeout)
        cs_opts = ConvertOptions(
            limits=cs_limits,
            qr_enabled=True,
            ocr_enabled=False,  # OCR is opt-in; keep fast for framework proof
        )

        converter = self._get_converter()
        result = converter.convert(input, outdir, cs_opts)
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
