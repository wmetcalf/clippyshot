"""HTTP tests for GET /v1/jobs/{id}/pdf."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clippyshot.api import build_app
from clippyshot.jobs import InMemoryJobStore
from clippyshot.jobs.base import Job, JobStatus


def _make_app_with_job(tmp_path, *, with_pdf: bool = True) -> tuple:
    """Create a minimal app + a DONE job whose output dir has (or doesn't have) document.pdf."""
    job_store = InMemoryJobStore()
    app = build_app(
        converter_factory=lambda: None,  # never called
        job_store=job_store,
    )
    out = tmp_path / "jobs" / "xyz" / "output"
    out.mkdir(parents=True)
    if with_pdf:
        (out / "document.pdf").write_bytes(b"%PDF-1.7\ntest body\n%%EOF")
    job = Job.new(filename="x.docx")
    job.status = JobStatus.DONE
    job.result_dir = str(out)
    job_store.create(job)
    return app, job.job_id


def test_pdf_endpoint_returns_pdf(tmp_path):
    app, job_id = _make_app_with_job(tmp_path, with_pdf=True)
    c = TestClient(app)
    r = c.get(f"/v1/jobs/{job_id}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-")


def test_pdf_endpoint_404_for_unknown_job(tmp_path):
    app, _ = _make_app_with_job(tmp_path, with_pdf=True)
    c = TestClient(app)
    r = c.get("/v1/jobs/doesnotexist/pdf")
    assert r.status_code == 404


def test_pdf_endpoint_410_when_pdf_missing(tmp_path):
    app, job_id = _make_app_with_job(tmp_path, with_pdf=False)
    c = TestClient(app)
    r = c.get(f"/v1/jobs/{job_id}/pdf")
    assert r.status_code == 410
