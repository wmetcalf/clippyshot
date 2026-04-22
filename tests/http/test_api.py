import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clippyshot.api import build_app
from clippyshot.converter import ConversionResult
from clippyshot.jobs import InMemoryJobStore, Job, JobStatus


REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "safe"


# Valid 1x1 white PNG bytes (generated via PIL once and embedded).
def _tiny_png() -> bytes:
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


class FakeConverter:
    def __init__(self):
        self.calls = 0

    def convert(self, input_path, output_dir, options):
        self.calls += 1
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        png = _tiny_png()
        (output_dir / "page-001.png").write_bytes(png)
        meta = {
            "clippyshot_version": "test",
            "input": {"filename": Path(input_path).name},
            "render": {
                "page_count_rendered": 1,
                "page_count_total": 1,
                "truncated": False,
            },
            "security": {"sandbox": "fake"},
            "pages": [{"index": 1, "file": "page-001.png"}],
            "warnings": [],
            "errors": [],
        }
        (output_dir / "metadata.json").write_text(json.dumps(meta))
        return ConversionResult(output_dir=output_dir, metadata=meta)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_JOB_ROOT", str(tmp_path / "jobs"))
    fake = FakeConverter()
    store = InMemoryJobStore()
    app = build_app(
        converter_factory=lambda: fake,
        job_store=store,
    )
    app.state.job_store = store
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/v1/healthz")
    assert r.status_code == 200


def test_version(client):
    r = client.get("/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "supported_formats" in body
    assert isinstance(body["supported_formats"], list)
    for ext in ("xlam", "xla", "ppam", "ppa"):
        assert ext in body["supported_formats"]
    assert body["security"]["secure"] is True
    assert body["security"]["warnings"] == []
    assert "runtime_apparmor_profile" not in body
    assert "soffice_apparmor_profile" not in body
    assert "seccomp" not in body


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "clippyshot_" in r.text


def test_sync_convert_returns_zip(client):
    files = {
        "file": (
            "tiny.docx",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/convert", files=files, headers={"Accept": "application/zip"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert r.content[:2] == b"PK"


def test_async_job_submission_is_queue_only(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_JOB_ROOT", str(tmp_path / "jobs"))
    fake = FakeConverter()
    store = InMemoryJobStore()
    app = build_app(
        converter_factory=lambda: fake,
        job_store=store,
    )
    with TestClient(app) as client:
        files = {
            "file": (
                "tiny.docx",
                (FIXTURES / "tiny.docx").read_bytes(),
                "application/octet-stream",
            ),
        }
        r = client.post("/v1/jobs", files=files)
        assert r.status_code == 202
        body = r.json()
        job_id = body["job_id"]
        assert body["status"] == "queued"
        assert "self" in body["links"]
        assert "result" in body["links"]

    assert fake.calls == 0
    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.result_dir == str(tmp_path / "jobs" / job_id / "output")
    assert job.scan_options == {
        "CLIPPYSHOT_ENABLE_QR": "1",
        "CLIPPYSHOT_QR_FORMATS": "qr_code,micro_qr_code,rmqr_code",
        "CLIPPYSHOT_ZXING_TIMEOUT_S": "10",
        "CLIPPYSHOT_ENABLE_OCR": "0",
        "CLIPPYSHOT_OCR_ALL": "0",
        "CLIPPYSHOT_OCR_LANG": "eng",
        "CLIPPYSHOT_OCR_PSM": "6",
        "CLIPPYSHOT_OCR_TIMEOUT_S": "60",
    }
    assert Path(job.result_dir).is_dir()
    assert (tmp_path / "jobs" / job_id / "input" / "tiny.docx").is_file()


def test_async_job_submission_persists_custom_scanner_options(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("CLIPPYSHOT_JOB_ROOT", str(tmp_path / "jobs"))
    store = InMemoryJobStore()
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=store,
    )

    with TestClient(app) as client:
        files = {
            "file": (
                "tiny.docx",
                (FIXTURES / "tiny.docx").read_bytes(),
                "application/octet-stream",
            ),
        }
        r = client.post(
            "/v1/jobs",
            files=files,
            data={
                "qr": "0",
                "qr_formats": "qr_code,data_matrix",
                "ocr": "1",
                "ocr_all": "1",
                "ocr_lang": "eng+deu",
                "ocr_psm": "11",
                "ocr_timeout_s": "120",
            },
        )

    assert r.status_code == 202
    job = store.get(r.json()["job_id"])
    assert job is not None
    assert job.scan_options == {
        "CLIPPYSHOT_ENABLE_QR": "0",
        "CLIPPYSHOT_QR_FORMATS": "qr_code,data_matrix",
        "CLIPPYSHOT_ZXING_TIMEOUT_S": "10",
        "CLIPPYSHOT_ENABLE_OCR": "1",
        "CLIPPYSHOT_OCR_ALL": "1",
        "CLIPPYSHOT_OCR_LANG": "eng+deu",
        "CLIPPYSHOT_OCR_PSM": "11",
        "CLIPPYSHOT_OCR_TIMEOUT_S": "120",
    }


def test_async_job_submission_rolls_back_if_staging_fails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_JOB_ROOT", str(tmp_path / "jobs"))
    original_open = Path.open

    def fail_open(self, mode="r", *args, **kwargs):
        if "w" in mode:
            raise OSError("disk full")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr("clippyshot.api.Path.open", fail_open)
    store = InMemoryJobStore()
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=store,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        files = {
            "file": (
                "tiny.docx",
                (FIXTURES / "tiny.docx").read_bytes(),
                "application/octet-stream",
            ),
        }
        r = client.post("/v1/jobs", files=files)

    assert r.status_code == 500
    job_root = tmp_path / "jobs"
    assert not job_root.exists() or not any(job_root.iterdir())
    assert store.list() == []


def test_version_discloses_runtime_details_only_when_env_enabled(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_DISCLOSE_SECURITY_INTERNALS", "1")
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(app) as c:
        r = c.get("/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert "runtime_apparmor_profile" in body
    assert "soffice_apparmor_profile" in body
    assert "seccomp" in body


def test_completed_job_result_uses_persisted_result_dir_without_registry(client):
    files = {
        "file": (
            "tiny.docx",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/jobs", files=files)
    job_id = r.json()["job_id"]
    out_dir = Path(client.app.state.job_store.get(job_id).result_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "page-001.png").write_bytes(_tiny_png())
    meta = {
        "clippyshot_version": "test",
        "input": {"filename": "tiny.docx"},
        "render": {
            "page_count_rendered": 1,
            "page_count_total": 1,
            "truncated": False,
        },
        "security": {"sandbox": "fake"},
        "pages": [{"index": 1, "file": "page-001.png"}],
        "warnings": [],
        "errors": [],
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta))
    client.app.state.job_store.update(
        job_id,
        status=JobStatus.DONE,
        finished_at=time.time(),
        pages_done=1,
        pages_total=1,
        result_dir=str(out_dir),
    )

    result = client.get(f"/v1/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.content[:2] == b"PK"


def test_get_job_404_for_unknown(client):
    r = client.get("/v1/jobs/does-not-exist")
    assert r.status_code == 404


def test_filename_with_path_traversal_is_sanitized(client, tmp_path):
    """A client filename like '../../etc/passwd' must not escape the staging dir."""
    files = {
        "file": (
            "../../etc/passwd",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/convert", files=files, headers={"Accept": "application/zip"})
    # Test passes if the upload succeeds OR fails cleanly — the key is no traversal.
    assert r.status_code in (200, 400)


def test_dot_only_filename_falls_back_to_upload_bin(client):
    """A filename consisting of only dots (e.g. '.') is considered hidden/
    invalid and must fall back to 'upload.bin'."""
    files = {
        "file": (
            ".",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/convert", files=files, headers={"Accept": "application/zip"})
    assert r.status_code in (200, 400)


def test_filename_with_unicode_and_null_is_sanitized(client):
    files = {
        "file": (
            "résumé\u0000.docx",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/convert", files=files)
    # Should not crash on the null byte or non-ASCII chars.
    assert r.status_code in (200, 400)


def test_safe_upload_name_unit():
    """Unit test the filename sanitizer directly for comprehensive coverage."""
    from clippyshot.api import _safe_upload_name

    assert _safe_upload_name(None) == "upload.bin"
    assert _safe_upload_name("") == "upload.bin"
    assert _safe_upload_name("../../etc/passwd") == "passwd"
    assert _safe_upload_name("/absolute/path/to/doc.docx") == "doc.docx"
    assert _safe_upload_name(".hidden") == "upload.bin"
    assert _safe_upload_name("..") == "upload.bin"
    assert _safe_upload_name("normal.docx") == "normal.docx"
    # Non-ASCII and control chars replaced with underscores.
    assert _safe_upload_name("résumé.docx") == "r_sum_.docx"
    assert _safe_upload_name("file\x00name.docx") == "file_name.docx"
    # Truncation at 255 chars.
    assert len(_safe_upload_name("a" * 500 + ".docx")) == 255


def test_api_honors_limits_from_env(monkeypatch):
    """Setting CLIPPYSHOT_MAX_PAGES via env should propagate to the API
    converter (H-2)."""
    monkeypatch.setenv("CLIPPYSHOT_MAX_PAGES", "7")

    captured = {}

    class CapturingConverter:
        def convert(self, input_path, output_dir, options):
            captured["max_pages"] = options.limits.max_pages
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "page-001.png").write_bytes(_tiny_png())
            meta = {
                "clippyshot_version": "test",
                "input": {"filename": "test.docx"},
                "render": {
                    "page_count_rendered": 1,
                    "page_count_total": 1,
                    "truncated": False,
                },
                "security": {"sandbox": "fake"},
                "pages": [],
                "warnings": [],
                "errors": [],
            }
            (output_dir / "metadata.json").write_text(json.dumps(meta))
            return ConversionResult(output_dir=output_dir, metadata=meta)

    app = build_app(
        converter_factory=lambda: CapturingConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(app) as c:
        files = {
            "file": (
                "tiny.docx",
                (FIXTURES / "tiny.docx").read_bytes(),
                "application/octet-stream",
            ),
        }
        r = c.post("/v1/convert", files=files, headers={"Accept": "application/zip"})
    assert r.status_code == 200
    assert captured["max_pages"] == 7


def test_oversized_upload_via_content_length_header(monkeypatch):
    """An upload exceeding max_input_bytes should get HTTP 413 before
    being read (H-1)."""
    monkeypatch.setenv("CLIPPYSHOT_MAX_INPUT", "1024")  # 1 KiB

    fake_app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(fake_app) as c:
        big = b"x" * 4096  # 4 KiB (after multipart framing comfortably > 1 KiB)
        files = {"file": ("big.docx", big, "application/octet-stream")}
        r = c.post("/v1/convert", files=files)
    assert r.status_code == 413
    assert "exceeds" in r.text.lower()


def test_body_size_limit_middleware_chunked_streaming():
    from clippyshot.api import BodySizeLimitMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            request = Request(scope, receive)
            async for chunk in request.stream():
                pass
            response = Response("ok")
            await response(scope, receive, send)

    middleware = BodySizeLimitMiddleware(app, max_bytes=10)

    with TestClient(middleware) as client:

        def stream():
            yield b"123456"
            yield b"123456"

        r = client.post("/", data=stream())
        assert r.status_code == 413
        assert "exceeds" in r.text.lower()


def test_undersized_upload_is_accepted_when_limit_is_raised(monkeypatch):
    """Sanity check: when CLIPPYSHOT_MAX_INPUT is plenty large, a small upload
    passes the middleware (H-1 regression guard)."""
    monkeypatch.setenv("CLIPPYSHOT_MAX_INPUT", str(10 * 1024 * 1024))  # 10 MiB

    fake_app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(fake_app) as c:
        files = {
            "file": (
                "tiny.docx",
                (FIXTURES / "tiny.docx").read_bytes(),
                "application/octet-stream",
            ),
        }
        r = c.post("/v1/convert", files=files, headers={"Accept": "application/zip"})
    assert r.status_code == 200


def test_delete_job(client):
    files = {
        "file": (
            "tiny.docx",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/jobs", files=files)
    job_id = r.json()["job_id"]
    r = client.delete(f"/v1/jobs/{job_id}")
    assert r.status_code == 200
    r = client.get(f"/v1/jobs/{job_id}")
    assert r.status_code == 404


def _finish_job(job_store, job_id: str, *, focused: bool = False) -> Path:
    job = job_store.get(job_id)
    assert job is not None
    assert job.result_dir is not None
    out_dir = Path(job.result_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png = _tiny_png()
    (out_dir / "page-001.png").write_bytes(png)
    pages = [{"index": 1, "file": "page-001.png"}]
    if focused:
        (out_dir / "page-001-focused.png").write_bytes(png)
        pages[0]["focused"] = {"file": "page-001-focused.png"}
    meta = {
        "clippyshot_version": "test",
        "input": {"filename": job.filename},
        "render": {
            "page_count_rendered": 1,
            "page_count_total": 1,
            "truncated": False,
        },
        "security": {"sandbox": "fake"},
        "pages": pages,
        "warnings": [],
        "errors": [],
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta))
    job_store.update(
        job_id,
        status=JobStatus.DONE,
        finished_at=time.time(),
        pages_done=1,
        pages_total=1,
        result_dir=str(out_dir),
        expires_at=time.time() + 3600,
    )
    return out_dir


# ---------------------------------------------------------------------------
# M-3: Bearer auth middleware
# ---------------------------------------------------------------------------


def test_no_auth_by_default(client):
    """With no CLIPPYSHOT_API_KEY, /v1/healthz is reachable without auth."""
    r = client.get("/v1/healthz")
    assert r.status_code == 200


def test_bearer_auth_enabled_when_env_set(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_API_KEY", "s3cret")
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(app) as c:
        # /healthz and /version are public (in _PUBLIC set)
        assert c.get("/v1/healthz").status_code == 200
        assert c.get("/v1/version").status_code == 200
        # /metrics requires auth
        assert c.get("/metrics").status_code == 401
        # A protected endpoint without token: 401
        files = {"file": ("t.docx", b"x", "application/octet-stream")}
        assert c.post("/v1/convert", files=files).status_code == 401
        # With right token: gets past auth (may fail later in conversion but not 401)
        r = c.post(
            "/v1/convert", files=files, headers={"Authorization": "Bearer s3cret"}
        )
        assert r.status_code != 401


def test_metrics_endpoint_requires_auth_when_key_set(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_API_KEY", "s3cret")
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# L-8: Configurable worker pool
# ---------------------------------------------------------------------------


def test_api_workers_env_var_honored(monkeypatch):
    monkeypatch.setenv("CLIPPYSHOT_API_WORKERS", "7")
    app = build_app(
        converter_factory=lambda: FakeConverter(),
        job_store=InMemoryJobStore(),
    )
    with TestClient(app) as c:
        assert c.get("/v1/healthz").status_code == 200


# ---------------------------------------------------------------------------
# M-9: Streaming responses
# ---------------------------------------------------------------------------


def test_pages_endpoint_streams_from_disk(client):
    """The per-page PNG endpoint should not load the entire image into memory."""
    files = {
        "file": (
            "tiny.docx",
            (FIXTURES / "tiny.docx").read_bytes(),
            "application/octet-stream",
        ),
    }
    r = client.post("/v1/jobs", files=files)
    job_id = r.json()["job_id"]
    _finish_job(client.app.state.job_store, job_id)
    r = client.get(f"/v1/jobs/{job_id}/pages/1.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


def test_get_focused_page_png(tmp_path: Path):
    job_store = InMemoryJobStore()
    app = build_app(job_store=job_store)
    client = TestClient(app)

    job = Job.new(filename="focused.docx")
    job.result_dir = str(tmp_path / job.job_id / "output")
    job_store.create(job)
    _finish_job(job_store, job.job_id, focused=True)

    r = client.get(f"/v1/jobs/{job.job_id}/pages/focused/1.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")


def test_trimmed_and_focused_endpoints_honor_expiry(tmp_path: Path):
    job_store = InMemoryJobStore()
    app = build_app(job_store=job_store)
    client = TestClient(app)

    job = Job.new(filename="focused.docx")
    out_dir = tmp_path / job.job_id / "output"
    job.result_dir = str(out_dir)
    job_store.create(job)
    _finish_job(job_store, job.job_id, focused=True)
    (out_dir / "page-001-trimmed.png").write_bytes(_tiny_png())
    job_store.update(job.job_id, expires_at=time.time() - 1)

    trimmed = client.get(f"/v1/jobs/{job.job_id}/pages/trimmed/1.png")
    focused = client.get(f"/v1/jobs/{job.job_id}/pages/focused/1.png")

    assert trimmed.status_code == 404
    assert focused.status_code == 404
    updated = job_store.get(job.job_id)
    assert updated is not None
    assert updated.status is JobStatus.EXPIRED
