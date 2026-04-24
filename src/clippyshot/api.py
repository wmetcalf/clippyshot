"""FastAPI HTTP server."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from clippyshot._version import __version__
from clippyshot.converter import ConvertOptions, Converter
from clippyshot.detector import Detector
from clippyshot.errors import (
    ConversionError,
    DetectionError,
    LibreOfficeEmptyOutputError,
    LibreOfficeError,
    RasterizeError,
    SandboxError,
    SandboxTimeout,
    SandboxUnavailable,
    sanitize_public_error,
)
from clippyshot.jobs import (
    InMemoryJobStore,
    Job,
    JobArtifactRegistry,
    JobStatus,
    JobStore,
    RedisJobStore,
    SqlJobStore,
)
from clippyshot.libreoffice.runner import LibreOfficeRunner
from clippyshot.limits import Limits
from clippyshot.observability import configure_logging, get_logger, set_sandbox_backend
from clippyshot.rasterizer.pdftoppm import PdftoppmRasterizer
from clippyshot.sandbox.detect import select_sandbox
from clippyshot.selftest import (
    detect_runtime_apparmor_profile,
    detect_soffice_apparmor_profile,
)


_log = get_logger("clippyshot.api")


def _conversion_error_stage(e: Exception) -> str:
    """Classify a conversion failure into a stable stage identifier.

    Used in the 422 response body so clients can tell where in the pipeline
    the failure happened without having to parse the error message.
    """
    if isinstance(e, LibreOfficeError):
        return "libreoffice"
    if isinstance(e, RasterizeError):
        return "rasterize"
    # ConversionError wraps a LO or rasterize error — inspect the cause.
    cause = getattr(e, "cause", None) or getattr(e, "__cause__", None)
    if isinstance(cause, LibreOfficeError):
        return "libreoffice"
    if isinstance(cause, RasterizeError):
        return "rasterize"
    return "unknown"


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_upload_name(raw: str | None) -> str:
    """Sanitize a client-supplied filename to a safe basename.

    - Strip directory components (Path.name)
    - Reject leading dots and empty results
    - Replace any character outside [A-Za-z0-9._-] with '_'
    - Truncate to 255 chars (POSIX NAME_MAX)
    - Fall back to 'upload.bin' if nothing usable remains
    """
    if not raw:
        return "upload.bin"
    base = Path(raw).name
    if not base or base.startswith("."):
        return "upload.bin"
    cleaned = _SAFE_FILENAME_RE.sub("_", base)[:255]
    return cleaned or "upload.bin"


async def _write_upload_to_path(upload: UploadFile, dest_path: Path) -> None:
    """Persist an UploadFile to disk without buffering the full body in memory."""
    with dest_path.open("wb") as f:
        while chunk := await upload.read(65536):
            f.write(chunk)


def _public_detail(exc: Exception | str) -> str:
    return sanitize_public_error(str(exc))


def _parse_bool(val: str | None, *, default: bool = False) -> bool:
    """Parse a bool-ish request param.

    Truthy: 1, true, yes, on, y (case-insensitive).
    Falsy: 0, false, no, off, n, empty string.
    None returns the default.
    """
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "on", "y"):
        return True
    if v in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _build_convert_options(
    *,
    qr: str | None = None,
    qr_formats: str | None = None,
    ocr: str | None = None,
    ocr_all: str | None = None,
    ocr_lang: str | None = None,
    ocr_psm: str | None = None,
    ocr_timeout_s: str | None = None,
) -> "ConvertOptions":
    """Assemble a ConvertOptions from optional string query params.

    Missing params fall through to env-var defaults (CLIPPYSHOT_*), then
    to the ConvertOptions dataclass defaults. Range validation:
    - ocr_psm must be in [0, 13]
    - ocr_timeout_s must be in [1, 600]
    """
    limits = Limits.from_env()
    env_qr = os.environ.get("CLIPPYSHOT_ENABLE_QR", "1")
    env_ocr = os.environ.get("CLIPPYSHOT_ENABLE_OCR", "0")
    env_lang = os.environ.get(
        "CLIPPYSHOT_OCR_LANG",
        "eng+Latin",
    )
    env_ocr_all = os.environ.get("CLIPPYSHOT_OCR_ALL", "0")

    qr_enabled = _parse_bool(qr, default=_parse_bool(env_qr, default=True))
    ocr_enabled = _parse_bool(ocr, default=_parse_bool(env_ocr, default=False))
    ocr_all_enabled = _parse_bool(
        ocr_all, default=_parse_bool(env_ocr_all, default=False)
    )

    psm_int = 3
    if ocr_psm is not None and ocr_psm != "":
        try:
            psm_int = int(ocr_psm)
        except ValueError:
            raise ValueError(f"ocr_psm must be an integer, got {ocr_psm!r}")
        if not 0 <= psm_int <= 13:
            raise ValueError(f"ocr_psm must be in [0, 13], got {psm_int}")

    timeout_int = int(os.environ.get("CLIPPYSHOT_OCR_TIMEOUT_S", "60"))
    if ocr_timeout_s is not None and ocr_timeout_s != "":
        try:
            timeout_int = int(ocr_timeout_s)
        except ValueError:
            raise ValueError(f"ocr_timeout_s must be an integer, got {ocr_timeout_s!r}")
    if not 1 <= timeout_int <= 600:
        raise ValueError(f"ocr_timeout_s must be in [1, 600], got {timeout_int}")

    qr_timeout_int = int(os.environ.get("CLIPPYSHOT_ZXING_TIMEOUT_S", "10"))

    return ConvertOptions(
        limits=limits,
        qr_enabled=qr_enabled,
        qr_formats=qr_formats or "qr_code,micro_qr_code,rmqr_code",
        qr_timeout_s=qr_timeout_int,
        ocr_enabled=ocr_enabled,
        ocr_all=ocr_all_enabled,
        ocr_lang=ocr_lang or env_lang,
        ocr_psm=psm_int,
        ocr_timeout_s=timeout_int,
    )


def _job_scan_env(opts: ConvertOptions) -> dict[str, str]:
    return {
        "CLIPPYSHOT_ENABLE_QR": "1" if opts.qr_enabled else "0",
        "CLIPPYSHOT_QR_FORMATS": opts.qr_formats,
        "CLIPPYSHOT_ZXING_TIMEOUT_S": str(opts.qr_timeout_s),
        "CLIPPYSHOT_ENABLE_OCR": "1" if opts.ocr_enabled else "0",
        "CLIPPYSHOT_OCR_ALL": "1" if opts.ocr_all else "0",
        "CLIPPYSHOT_OCR_LANG": opts.ocr_lang,
        "CLIPPYSHOT_OCR_PSM": str(opts.ocr_psm),
        "CLIPPYSHOT_OCR_TIMEOUT_S": str(opts.ocr_timeout_s),
    }


class BodySizeLimitMiddleware:
    """Reject requests whose body exceeds max_input_bytes.

    For requests with a Content-Length header, the check is O(1) — the limit
    is enforced before any body is read. For chunked uploads, the body is
    consumed in a streaming wrapper that aborts as soon as the running total
    exceeds the limit.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        cl = dict(scope.get("headers", [])).get(b"content-length")
        if cl is not None:
            try:
                if int(cl) > self._max:
                    response = PlainTextResponse(
                        f"request body exceeds {self._max} bytes", status_code=413
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = PlainTextResponse("invalid content-length", status_code=400)
                await response(scope, receive, send)
                return

        body_size = 0
        headers_sent = False

        async def wrapped_receive():
            nonlocal body_size
            msg = await receive()
            if msg["type"] == "http.request":
                body_size += len(msg.get("body", b""))
                if body_size > self._max:
                    raise RuntimeError("request_body_too_large")
            return msg

        async def wrapped_send(msg):
            nonlocal headers_sent
            if msg["type"] == "http.response.start":
                headers_sent = True
            await send(msg)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except RuntimeError as e:
            if str(e) == "request_body_too_large":
                if headers_sent:
                    # Cannot send 413 if the response already started.
                    return
                response = PlainTextResponse(
                    f"request body exceeds {self._max} bytes", status_code=413
                )
                await response(scope, receive, send)
                return
            raise


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Optional bearer-token authentication.

    Activated when CLIPPYSHOT_API_KEY is set in the environment. Every
    request to /v1/* (except /v1/healthz) and /metrics must include
    Authorization: Bearer <key> matching the env var. Comparisons are
    constant-time via hmac.compare_digest.
    """

    _PUBLIC = frozenset({"/v1/healthz", "/v1/version"})

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._key = api_key

    async def dispatch(self, request, call_next):
        if request.url.path in self._PUBLIC:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return PlainTextResponse("missing bearer token", status_code=401)
        provided = header.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(provided, self._key):
            return PlainTextResponse("invalid bearer token", status_code=401)
        return await call_next(request)


SUPPORTED_FORMATS = [
    # Microsoft Office OOXML
    "docx",
    "docm",
    "dotx",
    "dotm",
    "xlsx",
    "xlsm",
    "xltx",
    "xltm",
    "xlam",
    "xlsb",
    "pptx",
    "pptm",
    "ppsx",
    "ppsm",
    "potx",
    "potm",
    "ppam",
    # Microsoft Office legacy
    "doc",
    "dot",
    "xls",
    "xlt",
    "xla",
    "ppt",
    "pps",
    "pot",
    "ppa",
    # OpenDocument
    "odt",
    "ott",
    "fodt",
    "ods",
    "ots",
    "fods",
    "odp",
    "otp",
    "fodp",
    "odg",
    "otg",
    "fodg",
    # Text / markup
    "rtf",
    "txt",
    "csv",
    "md",
    # Microsoft XPS family
    "xps",
    "oxps",
    # Web / MIME HTML
    "html",
    "htm",
    "mht",
    "mhtml",
]


def _default_converter_factory() -> Converter:
    sandbox = select_sandbox()
    set_sandbox_backend(sandbox.name)
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


def _zip_dir_to_file(src_dir: Path, dest_file: Path) -> Path:
    """Stream zip writes to disk, always encrypted with the configured password.

    Uses pyzipper's AES-256 encryption. The password (default "infected")
    is a *signaling* convention for malware-derived artifacts — it's not
    a secrecy mechanism but a convention to prevent accidental extraction
    by analysts or AV engines.
    """
    import pyzipper

    password = os.environ.get("CLIPPYSHOT_ZIP_PASSWORD", "infected").encode("utf-8")
    with pyzipper.AESZipFile(
        dest_file,
        "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password)
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=f.relative_to(src_dir))
    return dest_file


def build_app(
    *,
    converter_factory: Callable[[], Converter] | None = None,
    job_store: JobStore | None = None,
    job_store_kind: str = "memory",
    redis_url: str | None = None,
    database_url: str | None = None,
) -> FastAPI:
    configure_logging()
    # Default to permanent persistence (0 = never expire). Operators who
    # want TTL-based cleanup set CLIPPYSHOT_JOB_RETENTION_SECONDS to a
    # positive value; when <=0 the sweeper skips jobs whose expires_at
    # field was never set by the dispatcher.
    job_retention_seconds = max(
        0,
        int(os.environ.get("CLIPPYSHOT_JOB_RETENTION_SECONDS", "0")),
    )

    if converter_factory is None:
        converter_factory = _default_converter_factory
    if job_store is None:
        if job_store_kind == "redis":
            import redis as _redis

            job_store = RedisJobStore(
                client=_redis.Redis.from_url(redis_url),
                ttl_seconds=job_retention_seconds,
            )
        elif job_store_kind == "sql":
            database_url = database_url or os.environ.get(
                "CLIPPYSHOT_DATABASE_URL",
                "sqlite:///./clippyshot-jobs.db",
            )
            job_store = SqlJobStore(database_url)
        else:
            job_store = InMemoryJobStore()

    converter_holder: dict = {}

    def get_converter() -> Converter:
        if "c" not in converter_holder:
            converter_holder["c"] = converter_factory()
        return converter_holder["c"]

    _api_workers = int(os.environ.get("CLIPPYSHOT_API_WORKERS", "4"))
    if _api_workers < 1:
        _api_workers = 1
    if _api_workers > 64:
        _api_workers = 64  # safety cap; raise if you really need more
    job_artifacts = JobArtifactRegistry(retention_seconds=job_retention_seconds)
    job_root = Path(
        os.environ.get("CLIPPYSHOT_JOB_ROOT", "/var/lib/clippyshot/jobs")
    ).expanduser()

    def _job_dirs(job_id: str) -> tuple[Path, Path, Path]:
        root = job_root / job_id
        return root, root / "input", root / "output"

    # Cache env-derived limits once: used both for the body-size middleware
    # and as the ConvertOptions default for /v1/convert and /v1/jobs.
    env_limits = Limits.from_env()

    app = FastAPI(title="ClippyShot", version=__version__)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=env_limits.max_input_bytes)

    cleanup_interval_s = min(max(5, max(job_retention_seconds, 1) // 4), 300)
    _sweeper_task: asyncio.Task | None = None

    def _job_output_dir(job_id: str, job: Job | None = None) -> Path | None:
        out = job_artifacts.path_for(job_id)
        if out is not None:
            return out
        if job is None:
            job = job_store.get(job_id)
        if job is None or job.result_dir is None:
            return None
        return Path(job.result_dir)

    def _safe_artifact_path(out: Path, filename: str) -> Path | None:
        """Resolve ``out / filename`` and return only if it stays under ``out``.

        Defense-in-depth against path traversal: today ``filename`` is a
        constant or derived from a typed int, but this keeps any future
        refactor that introduces user-controlled components from escaping
        the job's output directory.
        """
        try:
            out_resolved = out.resolve(strict=False)
            candidate = (out / filename).resolve(strict=False)
            candidate.relative_to(out_resolved)
        except (OSError, ValueError):
            return None
        return candidate

    async def _sweeper() -> None:
        while True:
            try:
                job_artifacts.expire_due(job_store)
            except Exception:
                _log.exception("sweeper iteration failed")
            await asyncio.sleep(cleanup_interval_s)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        nonlocal _sweeper_task
        _sweeper_task = asyncio.create_task(_sweeper())
        yield
        _sweeper_task.cancel()
        try:
            await _sweeper_task
        except asyncio.CancelledError:
            pass

    app.router.lifespan_context = _lifespan

    # Serve the web UI at /
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        # Serve the web UI at /
        @app.get("/", response_class=HTMLResponse)
        async def _ui_root():
            index = _static_dir / "index.html"
            # Cache-Control: no-store prevents browsers from pinning an old
            # copy of index.html across redeploys. The file is small and
            # regenerating it on every nav is cheap; the alternative is an
            # operator-reported "UI is stale" every time we ship UI changes.
            headers = {"Cache-Control": "no-store, must-revalidate"}
            if index.is_file():
                return HTMLResponse(index.read_text(), headers=headers)
            return HTMLResponse(
                "<h1>ClippyShot</h1><p>No UI found.</p>",
                headers=headers,
            )

        # Serve static assets (logo, etc.)
        assets_dir = _static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # M-3: optional bearer-token auth. Activated by CLIPPYSHOT_API_KEY.
    api_key = os.environ.get("CLIPPYSHOT_API_KEY", "").strip()
    if api_key:
        app.add_middleware(BearerAuthMiddleware, api_key=api_key)
        _log.info("api_auth_enabled", scheme="bearer")
    else:
        _log.warning(
            "api_auth_disabled",
            message=(
                "HTTP server has no authentication. Set CLIPPYSHOT_API_KEY "
                "or place behind an auth proxy. /v1/* and /metrics are open."
            ),
        )

    @app.get("/v1/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/v1/readyz")
    def readyz():
        try:
            get_converter()
            return {"status": "ready"}
        except SandboxUnavailable as e:
            raise HTTPException(status_code=503, detail=str(e))

    @app.get("/v1/version")
    def version():
        backend = "unknown"
        secure = True
        warnings: list[str] = []
        try:
            conv = get_converter()
            backend = conv._sandbox_backend  # noqa: SLF001
            sb = getattr(conv._runner, "_sandbox", None)  # noqa: SLF001
            secure = bool(getattr(sb, "secure", False))
            warnings = list(getattr(sb, "insecurity_reasons", []))
        except Exception:
            pass
        payload = {
            "version": __version__,
            "sandbox": backend,
            "supported_formats": SUPPORTED_FORMATS,
            "security": {
                "secure": secure,
                "warnings": warnings,
            },
        }
        if env_limits.disclose_security_internals:
            runtime_aa = detect_runtime_apparmor_profile()
            soffice_aa = "clippyshot-soffice"
            seccomp = "none"
            try:
                conv = get_converter()
                soffice_aa = conv._soffice_apparmor_profile  # noqa: SLF001
                seccomp = conv._seccomp  # noqa: SLF001
            except Exception:
                pass
            payload.update(
                {
                    "sandbox": backend,
                    "apparmor_profile": runtime_aa,
                    "runtime_apparmor_profile": runtime_aa,
                    "soffice_apparmor_profile": soffice_aa,
                    "seccomp": seccomp,
                }
            )
        return payload

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/convert")
    async def convert(
        file: UploadFile = File(...),
        qr: str | None = Form(default=None),
        qr_formats: str | None = Form(default=None),
        ocr: str | None = Form(default=None),
        ocr_all: str | None = Form(default=None),
        ocr_lang: str | None = Form(default=None),
        ocr_psm: str | None = Form(default=None),
        ocr_timeout_s: str | None = Form(default=None),
    ):
        safe_name = _safe_upload_name(file.filename)
        if safe_name != (file.filename or ""):
            _log.info(
                "upload_filename_sanitized",
                raw_filename=file.filename,
                safe_name=safe_name,
            )
        with tempfile.TemporaryDirectory(prefix="clippyshot-sync-") as tmp_str:
            tmp = Path(tmp_str)
            input_path = tmp / safe_name
            await _write_upload_to_path(file, input_path)
            out = tmp / "out"
            try:
                opts = _build_convert_options(
                    qr=qr,
                    qr_formats=qr_formats,
                    ocr=ocr,
                    ocr_all=ocr_all,
                    ocr_lang=ocr_lang,
                    ocr_psm=ocr_psm,
                    ocr_timeout_s=ocr_timeout_s,
                )
            except ValueError as e:
                return JSONResponse(
                    {"error": "invalid_parameter", "detail": _public_detail(e)},
                    status_code=400,
                )
            try:
                get_converter().convert(input_path, out, opts)
            except DetectionError as e:
                return JSONResponse(
                    {"error": e.reason, "detail": sanitize_public_error(e.detail)},
                    status_code=400,
                )
            except LibreOfficeEmptyOutputError as e:
                # soffice finished cleanly but wrote no output — typical
                # defensive outcome for malformed/exploit input.
                return JSONResponse(
                    {
                        "error": "conversion_produced_no_output",
                        "detail": _public_detail(e),
                    },
                    status_code=422,
                )
            except (LibreOfficeError, RasterizeError, ConversionError) as e:
                # The document exists but our pipeline could not render it.
                # 422 Unprocessable Entity is the correct status — the server
                # understood the request but the content is unrenderable.
                return JSONResponse(
                    {
                        "error": "conversion_failed",
                        "stage": _conversion_error_stage(e),
                        "detail": _public_detail(e),
                    },
                    status_code=422,
                )
            except SandboxTimeout as e:
                return JSONResponse(
                    {"error": "conversion_timeout", "detail": _public_detail(e)},
                    status_code=504,
                )
            except SandboxUnavailable as e:
                return JSONResponse(
                    {"error": "sandbox_unavailable", "detail": _public_detail(e)},
                    status_code=503,
                )
            except SandboxError as e:
                return JSONResponse(
                    {"error": "sandbox_error", "detail": _public_detail(e)},
                    status_code=503,
                )
            # M-9: stage zip to disk and stream via FileResponse.
            fd, tmp_zip_str = tempfile.mkstemp(
                prefix="clippyshot-sync-result-", suffix=".zip"
            )
            os.close(fd)
            tmp_zip = Path(tmp_zip_str)
            # Zip can be hundreds of MB of compressed work; push it off the
            # event loop so other API requests aren't blocked.
            await asyncio.to_thread(_zip_dir_to_file, out, tmp_zip)
            # The TemporaryDirectory context manager cleans up `tmp` (input +
            # output dir) when the `with` block exits.  The zip lives outside
            # that directory, so we clean it up via BackgroundTask after the
            # response is fully sent.
            return FileResponse(
                tmp_zip,
                media_type="application/zip",
                filename="result.zip",
                background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
            )

    @app.post("/v1/jobs", status_code=202)
    async def submit_job(
        file: UploadFile = File(...),
        qr: str | None = Form(default=None),
        qr_formats: str | None = Form(default=None),
        ocr: str | None = Form(default=None),
        ocr_all: str | None = Form(default=None),
        ocr_lang: str | None = Form(default=None),
        ocr_psm: str | None = Form(default=None),
        ocr_timeout_s: str | None = Form(default=None),
    ):
        # Validate scanner params up front so malformed values fail with 400
        # at submission rather than surfacing as an async job error later.
        try:
            opts = _build_convert_options(
                qr=qr,
                qr_formats=qr_formats,
                ocr=ocr,
                ocr_all=ocr_all,
                ocr_lang=ocr_lang,
                ocr_psm=ocr_psm,
                ocr_timeout_s=ocr_timeout_s,
            )
        except ValueError as e:
            return JSONResponse(
                {"error": "invalid_parameter", "detail": str(e)}, status_code=400
            )
        safe_name = _safe_upload_name(file.filename)
        if safe_name != (file.filename or ""):
            _log.info(
                "upload_filename_sanitized",
                raw_filename=file.filename,
                safe_name=safe_name,
            )
        job = Job.new(filename=safe_name)
        job.scan_options = _job_scan_env(opts)
        # expires_at is set by the dispatcher when the job reaches a
        # terminal state (DONE/FAILED), so the retention clock runs from
        # finish time. Until then, the job can sit in the queue as long
        # as it takes — retention only trims completed artifacts.

        root, input_dir, output_dir = _job_dirs(job.job_id)
        input_path = input_dir / safe_name
        try:
            root.mkdir(parents=True, exist_ok=True)
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            await _write_upload_to_path(file, input_path)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

        job.result_dir = str(output_dir)
        try:
            job_store.create(job)
            job_artifacts.register(job.job_id, output_dir)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

        return {
            "job_id": job.job_id,
            "status": "queued",
            "links": {
                "self": f"/v1/jobs/{job.job_id}",
                "result": f"/v1/jobs/{job.job_id}/result",
            },
        }

    @app.get("/v1/jobs")
    def list_jobs(
        offset: int = 0,
        limit: int = 100,
        status: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
        q: str | None = None,
    ):
        """List tracked jobs. Supports pagination, status filter, sorting, and filename search."""
        from clippyshot.jobs.base import JobStatus as _JS

        if status:
            try:
                filter_status = _JS(status)
            except ValueError:
                filter_status = None
            jobs = (
                job_store.list(status=filter_status)
                if filter_status
                else job_store.list()
            )
        else:
            jobs = job_store.list()

        # Filename substring search (case-insensitive)
        if q:
            q_lower = q.lower()
            jobs = [j for j in jobs if q_lower in (j.filename or "").lower()]

        # Sort
        reverse = order.lower() == "desc"
        if sort == "filename":
            jobs.sort(key=lambda j: (j.filename or "").lower(), reverse=reverse)
        elif sort == "ext":
            jobs.sort(
                key=lambda j: (
                    (j.filename or "").rsplit(".", 1)[-1].lower()
                    if "." in (j.filename or "")
                    else ""
                ),
                reverse=reverse,
            )
        elif sort == "status":
            jobs.sort(key=lambda j: j.status.value, reverse=reverse)
        elif sort == "pages":
            jobs.sort(key=lambda j: j.pages_done, reverse=reverse)
        else:
            jobs.sort(key=lambda j: j.created_at, reverse=reverse)

        total = len(jobs)
        page = jobs[offset : offset + limit]
        return {
            "jobs": [j.to_public_dict() for j in page],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str):
        job_artifacts.expire_due(job_store)
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job.to_public_dict()

    # ---- similarity search -------------------------------------------------

    def _phash_hex_to_int8(hex_str: str) -> int:
        """Signed int64 cast for a 16-char hex pHash (matches dispatcher)."""
        val = int(hex_str, 16)
        if val >= 1 << 63:
            val -= 1 << 64
        return val

    def _int8_to_phash_hex(v: int) -> str:
        return f"{v & ((1 << 64) - 1):016x}"

    def _similar_rows_to_response(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            ph = r.get("phash")
            out.append(
                {
                    "job_id": r["job_id"],
                    "page_index": r["page_index"],
                    "variant": r.get("variant", "original"),
                    "filename": r.get("filename"),
                    "phash": _int8_to_phash_hex(int(ph)) if ph is not None else None,
                    "colorhash": r.get("colorhash"),
                    "sha256": r.get("sha256"),
                    "distance": r.get("distance"),
                }
            )
        return out

    # Similarity queries (phash bktree + colorhash_bin_distance seq scan)
    # run directly in Postgres. A handful of concurrent fuzzy colorhash
    # calls can saturate the pg connection pool since the seq scan has no
    # usable index, so we gate all /v1/similar traffic through one
    # semaphore. Size is deliberately small: 3 is enough for normal UI
    # use (two tabs open, one in flight) but not enough for an
    # authenticated attacker to pin Postgres.
    _similar_gate = asyncio.Semaphore(3)

    @app.get("/v1/similar")
    async def get_similar(
        phash: str | None = None,
        colorhash: str | None = None,
        sha256: str | None = None,
        max_hamming: int = 5,
        colorhash_distance: int | None = None,
        colorhash_frac_max: int | None = None,
        colorhash_faint_max: int | None = None,
        colorhash_bright_max: int | None = None,
        limit: int = 50,
    ):
        """Find pages similar to a given hash.

        - phash: 16-char hex pHash; pages within ``max_hamming`` Hamming bits.
        - colorhash: 14-char hex; exact match by default. Pass any of the
          following to switch to per-bin L1 fuzzy search:
            - colorhash_distance: cap on total 14-bin L1 distance (simple).
            - colorhash_frac_max / colorhash_faint_max / colorhash_bright_max:
              per-group caps on bins 0–1, 2–7, 8–13 (advanced).
          Fuzzy-mode caps are cumulative — rows must satisfy every cap set.
        - sha256: 64-char hex SHA-256; exact match only.

        Exactly one of phash/colorhash/sha256 must be set.
        """
        provided = [p for p in (phash, colorhash, sha256) if p]
        if len(provided) != 1:
            raise HTTPException(400, "provide exactly one of phash, colorhash, sha256")
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be in [1, 500]")
        if not hasattr(job_store, "find_similar_phash"):
            raise HTTPException(
                501, "current job store does not support similarity search"
            )

        # Validate first so bad requests fail fast without consuming a
        # semaphore slot. DB work runs inside the gate via asyncio.to_thread
        # since the store APIs are synchronous (psycopg pool under the hood).
        if phash:
            if not re.fullmatch(r"[0-9a-fA-F]{16}", phash):
                raise HTTPException(400, "phash must be 16 hex chars")
            if not (0 <= max_hamming <= 64):
                raise HTTPException(400, "max_hamming must be in [0, 64]")
            async with _similar_gate:
                rows = await asyncio.to_thread(
                    job_store.find_similar_phash,
                    _phash_hex_to_int8(phash),
                    max_hamming,
                    limit=limit,
                )
        elif colorhash:
            if not re.fullmatch(r"[0-9a-fA-F]{14}", colorhash):
                raise HTTPException(400, "colorhash must be 14 hex chars")
            fuzzy_caps = {
                "total_max": colorhash_distance,
                "frac_max": colorhash_frac_max,
                "faint_max": colorhash_faint_max,
                "bright_max": colorhash_bright_max,
            }
            # Maxima derive from colorhash's binbits=4 layout: each bin is a
            # 4-bit count (0–15), so per-bin Δ ≤ 15. Groups:
            #   total = 14 bins → 210, frac = 2 bins → 30,
            #   faint/bright = 6 bins → 90 each.
            fuzzy_cap_limits = {
                "total_max": 210,
                "frac_max": 30,
                "faint_max": 90,
                "bright_max": 90,
            }
            for name, cap in fuzzy_caps.items():
                if cap is None:
                    continue
                ceiling = fuzzy_cap_limits[name]
                if not (0 <= cap <= ceiling):
                    raise HTTPException(400, f"{name} must be in [0, {ceiling}]")
            use_fuzzy = any(v is not None for v in fuzzy_caps.values())
            if use_fuzzy and not hasattr(job_store, "find_similar_colorhash"):
                raise HTTPException(
                    501, "current job store does not support colorhash fuzzy search"
                )
            async with _similar_gate:
                if use_fuzzy:
                    rows = await asyncio.to_thread(
                        job_store.find_similar_colorhash,
                        colorhash,
                        limit=limit,
                        **fuzzy_caps,
                    )
                else:
                    rows = await asyncio.to_thread(
                        job_store.find_by_colorhash,
                        colorhash,
                        limit=limit,
                    )
        else:
            if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
                raise HTTPException(400, "sha256 must be 64 hex chars")
            async with _similar_gate:
                rows = await asyncio.to_thread(
                    job_store.find_by_page_sha256,
                    sha256,
                    limit=limit,
                )
        return {"results": _similar_rows_to_response(rows)}

    @app.get("/v1/jobs/{job_id}/result")
    async def get_result(job_id: str):
        job_artifacts.expire_due(job_store)
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status != JobStatus.DONE:
            raise HTTPException(409, f"job not done (status={job.status.value})")
        out = _job_output_dir(job_id, job)
        if out is None or not out.exists():
            raise HTTPException(410, "result expired")
        # M-9: stage zip to a temp file and stream via FileResponse with
        # BackgroundTask cleanup so the zip is removed after delivery.
        fd, tmp_zip_str = tempfile.mkstemp(
            prefix=f"clippyshot-result-{job_id}-", suffix=".zip"
        )
        os.close(fd)
        tmp_zip = Path(tmp_zip_str)
        # Zip work (zlib deflate over potentially hundreds of MB of PNGs)
        # must not run on the asyncio event loop — it blocks every other
        # API request during the compress pass.
        if job_artifacts.path_for(job_id) is None:
            job_artifacts.register(job_id, out)
        if not job_artifacts.acquire(job_id):
            raise HTTPException(410, "result expired")
        try:
            await asyncio.to_thread(_zip_dir_to_file, out, tmp_zip)
        finally:
            job_artifacts.release(job_id)
        return FileResponse(
            tmp_zip,
            media_type="application/zip",
            filename=f"{job_id}.zip",
            background=BackgroundTask(tmp_zip.unlink, missing_ok=True),
        )

    @app.get("/v1/jobs/{job_id}/metadata")
    def get_metadata(job_id: str):
        job_artifacts.expire_due(job_store)
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status != JobStatus.DONE:
            raise HTTPException(409, f"job not done (status={job.status.value})")
        out = _job_output_dir(job_id, job)
        if out is None or not out.exists():
            raise HTTPException(410, "result expired")
        meta_json = out / "metadata.json"
        if not meta_json.exists():
            raise HTTPException(404, "metadata missing")
        return FileResponse(meta_json, media_type="application/json")

    @app.get("/v1/jobs/{job_id}/pdf")
    def get_pdf(job_id: str):
        """Stream the rendered document.pdf for a completed job."""
        job_artifacts.expire_due(job_store)
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status != JobStatus.DONE:
            raise HTTPException(409, f"job not done (status={job.status.value})")
        out = _job_output_dir(job_id, job)
        if out is None:
            raise HTTPException(410, "result expired")
        pdf = _safe_artifact_path(out, "document.pdf")
        if pdf is None or not pdf.is_file():
            raise HTTPException(410, "pdf missing; job may predate pdf preservation")
        return FileResponse(
            pdf,
            media_type="application/pdf",
            filename=f"{job_id}.pdf",
        )

    def _require_done_for_artifact(job_id: str) -> "Job":
        """Shared gate for artifact routes.

        - 404 if the id is unknown.
        - 410 if the job was DONE but retention has expired it (artifacts gone).
        - 409 if the job is still queued/running, or failed.
        - Passes through when status is DONE.

        The output directory is created at submit time and the worker
        writes pages incrementally, so without this check callers could
        fetch partial PNGs from RUNNING jobs or stale pages from FAILED
        jobs. Mirrors the gate the /metadata and /pdf routes apply.
        """
        job_artifacts.expire_due(job_store)
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        if job.status == JobStatus.EXPIRED:
            raise HTTPException(410, "result expired")
        if job.status != JobStatus.DONE:
            raise HTTPException(409, f"job not done (status={job.status.value})")
        return job

    @app.get("/v1/jobs/{job_id}/pages/trimmed/{idx}.png")
    def get_page_trimmed(job_id: str, idx: int):
        """Serve the trimmed version of a page (solid-color bottom removed)."""
        job = _require_done_for_artifact(job_id)
        out = _job_output_dir(job_id, job)
        if out is None:
            raise HTTPException(410, "result expired")
        png = _safe_artifact_path(out, f"page-{idx:03d}-trimmed.png")
        if png is None or not png.exists():
            raise HTTPException(404, "no trimmed version for this page")
        return FileResponse(png, media_type="image/png")

    @app.get("/v1/jobs/{job_id}/pages/focused/{idx}.png")
    def get_page_focused(job_id: str, idx: int):
        """Serve the focused version of a page (solid margins trimmed on all sides)."""
        job = _require_done_for_artifact(job_id)
        out = _job_output_dir(job_id, job)
        if out is None:
            raise HTTPException(410, "result expired")
        png = _safe_artifact_path(out, f"page-{idx:03d}-focused.png")
        if png is None or not png.exists():
            raise HTTPException(404, "no focused version for this page")
        return FileResponse(png, media_type="image/png")

    @app.get("/v1/jobs/{job_id}/pages/{idx}.png")
    def get_page(job_id: str, idx: int):
        job = _require_done_for_artifact(job_id)
        out = _job_output_dir(job_id, job)
        if out is None:
            raise HTTPException(410, "result expired")
        png = _safe_artifact_path(out, f"page-{idx:03d}.png")
        if png is None or not png.exists():
            raise HTTPException(404, "page not yet rendered")
        return FileResponse(png, media_type="image/png")

    @app.delete("/v1/jobs/{job_id}")
    def delete_job(job_id: str):
        """Remove a job's store entry and artifacts.

        Refuses to delete a job that's still queued or running — the
        dispatcher writes terminal state back to the row when the worker
        finishes; deleting the row out from under it would raise inside
        the dispatch loop and orphan the worker container.

        404s if neither the store nor the artifact registry knows the id.
        """
        job = job_store.get(job_id)
        artifact_path = job_artifacts.path_for(job_id)
        if job is None and artifact_path is None:
            raise HTTPException(404, "job not found")
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise HTTPException(
                409,
                f"cannot delete job in status={job.status.value}; wait for it to finish",
            )
        if job_artifacts.in_use(job_id):
            raise HTTPException(
                409, "job artifacts are in use; retry after download completes"
            )
        out = _job_output_dir(job_id, job)
        job_artifacts.delete(job_id)
        if out is not None:
            shutil.rmtree(out.parent, ignore_errors=True)
        job_store.delete(job_id)
        return {"deleted": job_id}

    return app
