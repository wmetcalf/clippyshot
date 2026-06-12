"""ClippyShot product routes for the shared blastbox ingress.

Mounts ClippyShot's typed-artifact routes (``/pdf`` + the per-page PNG variants)
on top of the generic blastbox ingress via the ``IngressExtension`` seam,
resolved by ``BLASTBOX_INGRESS_EXTENSION=clippyshot.blastbox_ingress:make_extension``.

Each handler is a thin shim over ``request.app.state.serve_artifact_file`` — the
core helper that already replicates ClippyShot's artifact confinement (DONE-gate,
``resolve()+relative_to()`` containment, no-symlink-follow). The core owns auth +
limits + path-confinement; these routers add no security logic of their own.

Only the *fixed-filename* artifact routes live here. ``/convert`` (synchronous,
in-process pipeline) and ``/similar`` (perceptual-hash search) are deliberately
NOT ported — the former is incompatible with the host's submit→dispatch→poll
split, and the latter is an image-hashing concern that belongs to ClippyShot's
``SqlJobStore``, not the generic host.
"""

from __future__ import annotations

from pathlib import Path as _FsPath

from fastapi import APIRouter, Path, Request
from fastapi.responses import FileResponse

from blastbox.host.ingress.extension import IngressExtension, StaticUI

router = APIRouter()

# ClippyShot's packaged web UI (served at GET / + /assets via the StaticUI seam).
_STATIC_DIR = _FsPath(__file__).resolve().parent / "static"


@router.get("/v1/jobs/{job_id}/pdf")
def get_pdf(job_id: str, request: Request) -> FileResponse:
    """Stream the rendered ``document.pdf`` for a completed job."""
    return request.app.state.serve_artifact_file(
        job_id,
        "document.pdf",
        media_type="application/pdf",
        filename=f"{job_id}.pdf",
    )


@router.get("/v1/jobs/{job_id}/pages/{idx}.png")
def get_page(job_id: str, request: Request, idx: int = Path(..., ge=1)) -> FileResponse:
    """Serve a rendered page PNG."""
    return request.app.state.serve_artifact_file(
        job_id,
        f"page-{idx:03d}.png",
        media_type="image/png",
    )


@router.get("/v1/jobs/{job_id}/pages/trimmed/{idx}.png")
def get_page_trimmed(job_id: str, request: Request, idx: int = Path(..., ge=1)) -> FileResponse:
    """Serve the trimmed version of a page (solid-color bottom removed)."""
    return request.app.state.serve_artifact_file(
        job_id,
        f"page-{idx:03d}-trimmed.png",
        media_type="image/png",
    )


@router.get("/v1/jobs/{job_id}/pages/focused/{idx}.png")
def get_page_focused(job_id: str, request: Request, idx: int = Path(..., ge=1)) -> FileResponse:
    """Serve the focused version of a page (solid margins trimmed on all sides)."""
    return request.app.state.serve_artifact_file(
        job_id,
        f"page-{idx:03d}-focused.png",
        media_type="image/png",
    )


def make_extension() -> IngressExtension:
    """Factory resolved by ``BLASTBOX_INGRESS_EXTENSION``.

    Returns an :class:`IngressExtension` carrying ClippyShot's typed-artifact
    routers AND its packaged web UI, mounted on the shared blastbox ingress by
    ``build_app`` (the UI at ``GET /`` + ``/assets``; per-engine UI seam).
    """
    static_ui = (
        StaticUI(directory=str(_STATIC_DIR))
        if (_STATIC_DIR / "index.html").is_file()
        else None
    )
    return IngressExtension(routers=(router,), static_ui=static_ui)
