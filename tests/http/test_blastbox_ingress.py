"""Tests for ClippyShot's blastbox ingress extension (typed-artifact routes).

Builds the shared blastbox ingress with ClippyShot's ``IngressExtension`` mounted
and asserts the product routes (``/pdf`` + per-page PNG variants) serve the fixed
artifact files from a DONE job's output dir — reusing the core's confinement via
``app.state.serve_artifact_file``.

Artifacts + a sealed ``metadata.json`` live under ``<job_root>/<id>/output`` — and
this test passes ``job_root=tmp_path/"jobs"``, so the on-disk path is
``tmp_path/jobs/<id>/output`` (mirroring ``blastbox/tests/host/ingress/test_app.py::_make_done_job``).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("blastbox.host.ingress.app")

from blastbox.contract.envelope import DeclaredArtifact, seal_envelope
from blastbox.contract.leaf import ArtifactRef, Detection, Dimensions
from blastbox.contract.nodes import Page
from blastbox.host.ingress.app import build_app
from blastbox.host.jobs.base import Job, JobStatus
from blastbox.host.jobs.memory import InMemoryJobStore

from clippyshot.blastbox_ingress import make_extension

_PDF_BYTES = b"%PDF-1.7\n%%clippyshot-test\n"
_PAGE_BYTES = b"\x89PNG\r\n\x1a\nPAGE-001"


def _make_client(tmp_path: Path) -> tuple[TestClient, InMemoryJobStore]:
    store = InMemoryJobStore()
    app = build_app(
        job_store=store,
        job_root=tmp_path / "jobs",
        allowed_engines={"clippyshot"},
        extension=make_extension(),
    )
    return TestClient(app, raise_server_exceptions=False), store


def _make_done_job(tmp_path: Path, store: InMemoryJobStore) -> tuple[Job, Path]:
    """Create a DONE clippyshot job with document.pdf + page-001.png on disk.

    Output goes under ``<job_root>/<id>/output``; the test's ``job_root`` is
    ``tmp_path/"jobs"`` (mirrors blastbox's own _make_done_job).
    """
    job = Job.new(engine="clippyshot", filename="test.docx")
    output_dir = tmp_path / "jobs" / job.job_id / "output"
    output_dir.mkdir(parents=True)

    (output_dir / "document.pdf").write_bytes(_PDF_BYTES)
    (output_dir / "page-001.png").write_bytes(_PAGE_BYTES)

    detection = Detection(
        label="docx",
        mime="application/vnd.openxmlformats",
        confidence=0.99,
        source="magika",
    )
    payload = Page(
        index=0,
        dims=Dimensions(width=210, height=297, unit="mm"),
        image=ArtifactRef(id="page-001"),
    )
    env = seal_envelope(
        engine="clippyshot",
        outdir=output_dir,
        input_sha256="a" * 64,
        detected=detection,
        declared=[DeclaredArtifact(id="page-001", path="page-001.png", kind="image")],
        warnings=[],
        payload=payload,
    )
    (output_dir / "metadata.json").write_text(env.model_dump_json(by_alias=True))

    job.result_dir = str(output_dir)
    job.input_sha256 = "a" * 64
    job.status = JobStatus.DONE
    job.finished_at = time.time()
    store.create(job)
    return job, output_dir


def test_pdf_route_served(tmp_path):
    client, store = _make_client(tmp_path)
    job, _ = _make_done_job(tmp_path, store)
    resp = client.get(f"/v1/jobs/{job.job_id}/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == _PDF_BYTES


def test_page_png_route_served(tmp_path):
    client, store = _make_client(tmp_path)
    job, _ = _make_done_job(tmp_path, store)
    resp = client.get(f"/v1/jobs/{job.job_id}/pages/1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == _PAGE_BYTES


def test_missing_page_png_returns_404(tmp_path):
    client, store = _make_client(tmp_path)
    job, _ = _make_done_job(tmp_path, store)
    # page-099.png was never written
    resp = client.get(f"/v1/jobs/{job.job_id}/pages/99.png")
    assert resp.status_code == 404


def test_missing_trimmed_and_focused_return_404(tmp_path):
    client, store = _make_client(tmp_path)
    job, _ = _make_done_job(tmp_path, store)
    # Neither trimmed nor focused variants were written for page 1.
    assert client.get(f"/v1/jobs/{job.job_id}/pages/trimmed/1.png").status_code == 404
    assert client.get(f"/v1/jobs/{job.job_id}/pages/focused/1.png").status_code == 404


def test_trimmed_and_focused_served_when_present(tmp_path):
    client, store = _make_client(tmp_path)
    job, output_dir = _make_done_job(tmp_path, store)
    (output_dir / "page-001-trimmed.png").write_bytes(b"TRIMMED")
    (output_dir / "page-001-focused.png").write_bytes(b"FOCUSED")

    rt = client.get(f"/v1/jobs/{job.job_id}/pages/trimmed/1.png")
    assert rt.status_code == 200
    assert rt.headers["content-type"] == "image/png"
    assert rt.content == b"TRIMMED"

    rf = client.get(f"/v1/jobs/{job.job_id}/pages/focused/1.png")
    assert rf.status_code == 200
    assert rf.headers["content-type"] == "image/png"
    assert rf.content == b"FOCUSED"


def test_page_idx_below_one_is_422(tmp_path):
    client, store = _make_client(tmp_path)
    job, _ = _make_done_job(tmp_path, store)
    # idx is Path(ge=1): a zero/negative page index (would format to a malformed
    # page filename) is rejected at the route boundary before any disk lookup.
    for path in (
        f"/v1/jobs/{job.job_id}/pages/0.png",
        f"/v1/jobs/{job.job_id}/pages/trimmed/0.png",
        f"/v1/jobs/{job.job_id}/pages/focused/0.png",
    ):
        assert client.get(path).status_code == 422


def test_pdf_route_409_when_not_done(tmp_path):
    """The core DONE-gate (via serve_artifact_file) applies to product routes too."""
    client, store = _make_client(tmp_path)
    job = Job.new(engine="clippyshot", filename="test.docx")
    store.create(job)  # QUEUED
    resp = client.get(f"/v1/jobs/{job.job_id}/pdf")
    assert resp.status_code == 409


def test_routes_404_for_unknown_job(tmp_path):
    client, _ = _make_client(tmp_path)
    import uuid

    jid = str(uuid.uuid4())
    assert client.get(f"/v1/jobs/{jid}/pdf").status_code == 404
    assert client.get(f"/v1/jobs/{jid}/pages/1.png").status_code == 404


def test_web_ui_served_at_root(tmp_path):
    """The packaged web UI is served via the StaticUI seam on the extension
    (per-engine UI) — GET / returns the ClippyShot index.html, not a 404."""
    client, _ = _make_client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text.lower()
    assert "<!doctype" in body or "<html" in body
