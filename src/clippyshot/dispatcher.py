"""Dispatcher loop for launching one worker container per queued job."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable, Sequence

from clippyshot.errors import sanitize_public_error
from clippyshot.jobs import Job, JobStatus, JobStore
from clippyshot.runtime.docker_runtime import (
    DockerRuntimeSelection,
    build_worker_docker_run_argv,
    select_worker_runtime,
)


_log = logging.getLogger("clippyshot.dispatcher")
_DEFAULT_JOB_ROOT = Path("/var/lib/clippyshot/jobs")
_DEFAULT_WORKER_COMMAND = ("worker",)


def _phash_hex_to_int8(hex_str: str) -> int:
    """Convert a 16-char hex pHash to a signed int64 suitable for Postgres BIGINT.

    imagehash emits the phash as unsigned 64-bit hex (e.g. "ff00aa...");
    Postgres BIGINT is signed two's-complement, so values with the high
    bit set must be reinterpreted as negative to fit. The same mapping
    gets applied on query so round-trips are exact.
    """
    val = int(hex_str, 16)
    if val >= 1 << 63:
        val -= 1 << 64
    return val


class Dispatcher:
    """Claim queued jobs and execute them in worker containers."""

    def __init__(
        self,
        *,
        job_store: JobStore,
        image_name: str,
        runtime_selector: Callable[[], DockerRuntimeSelection] = select_worker_runtime,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        job_root: Path | str = _DEFAULT_JOB_ROOT,
        worker_command: Sequence[str] = _DEFAULT_WORKER_COMMAND,
        storage_root: Path | str = Path("/var/lib/clippyshot"),
        max_parallel_jobs: int = 1,
        worker_timeout_s: int = 600,
        job_retention_seconds: int | None = None,
    ) -> None:
        self._job_store = job_store
        self._image_name = image_name
        self._runtime_selector = runtime_selector
        self._subprocess_runner = subprocess_runner
        self._job_root = Path(job_root)
        self._worker_command = tuple(worker_command)
        self._storage_root = Path(storage_root)
        self._max_parallel_jobs = max(1, int(max_parallel_jobs))
        self._worker_timeout_s = max(60, int(worker_timeout_s))
        # Retention clock runs from finish time, not submit time.
        # Default is 0 (permanent persistence — no automatic expiry).
        # Operators who want TTL-based cleanup set
        # CLIPPYSHOT_JOB_RETENTION_SECONDS to a positive value.
        if job_retention_seconds is None:
            job_retention_seconds = int(
                os.environ.get("CLIPPYSHOT_JOB_RETENTION_SECONDS", "0")
            )
        self._job_retention_seconds = max(0, int(job_retention_seconds))

    def dispatch_once(self) -> bool:
        """Claim and process the next queued job, if any."""
        job = self._job_store.claim_next()
        if job is None:
            return False
        self._dispatch_claimed_job(job)
        return True

    def run_forever(self, poll_interval_s: float = 5.0) -> None:
        """Continuously claim and dispatch jobs until interrupted."""
        with ThreadPoolExecutor(max_workers=self._max_parallel_jobs) as executor:
            active: dict[Future[None], str] = {}
            while True:
                active = self._reap_completed(active)
                self._requeue_orphaned_jobs(exclude_job_ids=set(active.values()))
                while len(active) < self._max_parallel_jobs:
                    job = self._job_store.claim_next()
                    if job is None:
                        break
                    future = executor.submit(self._dispatch_claimed_job, job)
                    active[future] = job.job_id
                if active:
                    done, _ = wait(
                        set(active),
                        timeout=poll_interval_s,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in done:
                        future.result()
                        active.pop(future, None)
                else:
                    time.sleep(poll_interval_s)

    def _dispatch_claimed_job(self, job: Job) -> None:
        job_dir = self._resolve_job_dir(job)
        output_dir = self._resolve_output_dir(job, job_dir)
        input_path = self._resolve_input_path(job, job_dir)

        try:
            runtime = self._runtime_selector()
        except Exception as exc:  # noqa: BLE001
            self._fail_job(job, output_dir, f"runtime selection failed: {exc}")
            self._cleanup_input(input_path)
            return

        warnings = self._merge_warnings(job.security_warnings, runtime.warnings)
        self._job_store.update(
            job.job_id,
            worker_runtime=runtime.runtime,
            security_warnings=warnings,
            result_dir=str(output_dir),
        )

        host_input_path = self._resolve_host_path(input_path)
        host_output_dir = self._resolve_host_path(output_dir)
        worker_argv = self._build_worker_argv(job)
        container_name = self._worker_container_name(job)
        docker_argv = build_worker_docker_run_argv(
            image=self._image_name,
            input_path=host_input_path,
            input_mount_path=f"/tmp/input/{input_path.name}",
            output_dir=host_output_dir,
            output_mount_path="/tmp/output",
            worker_argv=worker_argv,
            runtime=runtime,
            container_name=container_name,
            labels={"clippyshot.role": "worker", "clippyshot.job_id": job.job_id},
            extra_env=self._build_worker_env(job),
        )

        try:
            # Wall-clock cap on the docker run. If the worker hangs (daemon
            # dead, stuck process), we kill the container and fail the job
            # instead of pinning this dispatcher slot forever.
            completed = self._subprocess_runner(
                docker_argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._worker_timeout_s,
            )
        except subprocess.TimeoutExpired:
            self._kill_container(container_name)
            self._fail_job(
                job,
                output_dir,
                f"worker timed out after {self._worker_timeout_s}s",
            )
            self._cleanup_input(input_path)
            return
        except Exception as exc:  # noqa: BLE001
            self._fail_job(job, output_dir, f"docker launch failed: {exc}")
            self._cleanup_input(input_path)
            return

        finished_at = time.time()
        expires_at = (
            finished_at + self._job_retention_seconds
            if self._job_retention_seconds > 0
            else None
        )
        try:
            if completed.returncode == 0:
                metadata = self._read_metadata(output_dir)
                if metadata is None:
                    self._job_store.update(
                        job.job_id,
                        status=JobStatus.FAILED,
                        finished_at=finished_at,
                        expires_at=expires_at,
                        error="worker exited successfully but metadata.json was missing or invalid",
                        result_dir=str(output_dir),
                        worker_runtime=runtime.runtime,
                        security_warnings=warnings,
                    )
                    return
                pages_done, pages_total = self._page_counts(metadata, output_dir)
                input_sha256 = None
                try:
                    raw = metadata.get("input", {}).get("sha256")
                    if (
                        isinstance(raw, str)
                        and len(raw) == 64
                        and all(c in "0123456789abcdef" for c in raw.lower())
                    ):
                        input_sha256 = raw.lower()
                except Exception:
                    pass
                self._job_store.update(
                    job.job_id,
                    status=JobStatus.DONE,
                    finished_at=finished_at,
                    expires_at=expires_at,
                    pages_done=pages_done,
                    pages_total=pages_total,
                    error=None,
                    result_dir=str(output_dir),
                    worker_runtime=runtime.runtime,
                    security_warnings=warnings,
                    input_sha256=input_sha256,
                )
                # Fan out perceptual hashes to the page_hashes table for
                # similarity search. Best-effort — a JobStore that doesn't
                # implement upsert_page_hashes (in-memory / older SQLite)
                # just won't populate the index.
                upsert = getattr(self._job_store, "upsert_page_hashes", None)
                if callable(upsert):
                    rows: list[dict] = []
                    for p in metadata.get("pages", []):
                        idx = p.get("index")
                        if idx is None:
                            continue
                        # Emit one row per available variant (original +
                        # trimmed + focused if present). Each has its own
                        # phash / colorhash / sha256 computed at render time.
                        for variant, src in (
                            ("original", p),
                            ("trimmed", p.get("trimmed") or {}),
                            ("focused", p.get("focused") or {}),
                        ):
                            ph_hex = src.get("phash")
                            ch = src.get("colorhash")
                            sha = src.get("sha256")
                            if not (ph_hex and ch and sha):
                                continue
                            try:
                                ph_int = _phash_hex_to_int8(ph_hex)
                            except (TypeError, ValueError):
                                continue
                            rows.append(
                                {
                                    "page_index": int(idx),
                                    "variant": variant,
                                    "phash": ph_int,
                                    "colorhash": ch,
                                    "sha256": sha,
                                }
                            )
                    if rows:
                        try:
                            upsert(job.job_id, rows)
                        except Exception as e:
                            _log.warning(
                                "page_hash_upsert_failed job_id=%s error=%s",
                                job.job_id,
                                str(e)[:200],
                            )
                return
            self._job_store.update(
                job.job_id,
                status=JobStatus.FAILED,
                finished_at=finished_at,
                expires_at=expires_at,
                error=self._format_failure(completed),
                result_dir=str(output_dir),
                worker_runtime=runtime.runtime,
                security_warnings=warnings,
            )
        finally:
            # Always delete the uploaded malware sample after conversion —
            # regardless of success/failure. Only the output directory needs
            # to persist for artifact retrieval.
            self._cleanup_input(input_path)

    def _cleanup_input(self, input_path: Path) -> None:
        """Delete the malware input file + its containing input/ directory."""
        try:
            input_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            # Remove the (now-empty) input/ directory.
            input_path.parent.rmdir()
        except OSError:
            pass

    def _kill_container(self, container_name: str) -> None:
        """Best-effort `docker kill` on a stuck worker."""
        try:
            self._subprocess_runner(
                ["docker", "kill", container_name],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            pass

    def _reap_completed(
        self, active: dict[Future[None], str]
    ) -> dict[Future[None], str]:
        keep: dict[Future[None], str] = {}
        for future, job_id in active.items():
            if future.done():
                future.result()
            else:
                keep[future] = job_id
        return keep

    def _requeue_orphaned_jobs(self, exclude_job_ids: set[str] | None = None) -> int:
        exclude = exclude_job_ids or set()
        active_job_ids = self._list_active_worker_job_ids()
        if active_job_ids is None:
            return 0
        recovered = 0
        for job in self._job_store.list(status=JobStatus.RUNNING):
            if job.job_id in exclude or job.job_id in active_job_ids:
                continue
            self._job_store.update(
                job.job_id,
                status=JobStatus.QUEUED,
                started_at=None,
                worker_runtime=None,
                error=None,
            )
            recovered += 1
        return recovered

    def _list_active_worker_job_ids(self) -> set[str] | None:
        try:
            proc = self._subprocess_runner(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "label=clippyshot.role=worker",
                    "--format",
                    '{{.Label "clippyshot.job_id"}}',
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def _worker_container_name(self, job: Job) -> str:
        return f"clippyshot-worker-{job.job_id[:12]}"

    def _resolve_job_dir(self, job: Job) -> Path:
        if job.result_dir:
            return Path(job.result_dir).expanduser().resolve(strict=False).parent
        return self._job_root / job.job_id

    def _resolve_output_dir(self, job: Job, job_dir: Path) -> Path:
        if job.result_dir:
            return Path(job.result_dir).expanduser().resolve(strict=False)
        return job_dir / "output"

    def _resolve_host_path(self, path: Path) -> Path:
        candidate = path.expanduser().resolve(strict=False)
        env_root = os.environ.get("CLIPPYSHOT_HOST_STORAGE_ROOT")
        host_root = Path(env_root) if env_root else self._discover_host_storage_root()
        try:
            rel = candidate.relative_to(self._storage_root)
        except ValueError:
            return candidate
        return host_root / rel

    def _discover_host_storage_root(self) -> Path:
        container_id = os.environ.get("HOSTNAME", "").strip()
        if not container_id:
            return self._storage_root
        try:
            proc = self._subprocess_runner(
                ["docker", "inspect", container_id, "--format", "{{json .Mounts}}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return self._storage_root
        if proc.returncode != 0:
            return self._storage_root
        try:
            mounts = json.loads(proc.stdout)
        except Exception:
            return self._storage_root
        for mount in mounts or []:
            if mount.get("Destination") == str(self._storage_root) and mount.get(
                "Source"
            ):
                return Path(mount["Source"])
        return self._storage_root

    def _resolve_input_path(self, job: Job, job_dir: Path) -> Path:
        return job_dir / "input" / Path(job.filename).name

    def _build_worker_argv(self, job: Job) -> list[str]:
        input_path = f"/tmp/input/{Path(job.filename).name}"
        output_dir = "/tmp/output"
        argv = [
            *self._worker_command,
            "--job-dir",
            "/tmp",
            "--input",
            input_path,
            "--output",
            output_dir,
        ]
        argv.extend(["--job-id", job.job_id, "--quiet"])
        return argv

    def _build_worker_env(self, job: Job) -> dict[str, str]:
        scan_options = job.scan_options or {}
        if not isinstance(scan_options, dict):
            return {}
        return {
            str(name): str(value)
            for name, value in scan_options.items()
            if value is not None
        }

    def _read_metadata(self, output_dir: Path) -> dict | None:
        """Load and validate metadata.json written by the worker.

        **Trust boundary:** the worker container is sandboxed, but a
        malicious document that exploits soffice (uid 10001 inside the
        worker) can write arbitrary JSON to `/tmp/output/metadata.json`.
        We must treat the file as untrusted input even though we
        authored the producing code, because the producing code runs
        adjacent to attacker-controlled parser surface.

        Validation rules:
        - Top-level must be an object with bounded size (<1MB).
        - `pages` must be a list whose entries have string `file`
          fields matching the safe basename pattern (no path traversal,
          no symlinks out of output_dir).
        - Numeric fields are coerced and bounded.
        - Unknown top-level keys are preserved but not trusted for
          routing decisions.
        """
        meta_path = output_dir / "metadata.json"
        if not meta_path.is_file():
            return None
        try:
            size = meta_path.stat().st_size
        except OSError:
            return None
        if size <= 0 or size > 1 << 20:  # 1MB hard cap
            return None
        try:
            raw = meta_path.read_text(encoding="utf-8", errors="strict")
            meta = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        if not self._validate_metadata(meta, output_dir):
            return None
        return meta

    _SAFE_PAGE_FILE = re.compile(r"^page-\d{1,4}(-trimmed|-focused)?\.png$")
    # Hash fields flow from worker metadata.json into the DB and are later
    # echoed back to the browser as clickable hashes. The UI renders them
    # inside inline onclick="..." attributes, so an attacker-controlled
    # string with HTML entities could escape the JS literal via attribute
    # decoding. Enforce fixed-length hex here so the untrusted-worker →
    # DB → UI chain can't introduce non-hex content in the first place.
    _PHASH_HEX = re.compile(r"^[0-9a-fA-F]{16}$")
    _COLORHASH_HEX = re.compile(r"^[0-9a-fA-F]{14}$")
    _SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")

    def _hash_fields_valid(self, src: dict) -> bool:
        """Return True iff phash/colorhash/sha256 on ``src`` are missing or hex.

        A valid page can omit all three (e.g. blank pages short-circuit
        before hashing). If any are present they must match the canonical
        hex form; otherwise we reject the whole metadata.json.
        """
        for key, pattern in (
            ("phash", self._PHASH_HEX),
            ("colorhash", self._COLORHASH_HEX),
            ("sha256", self._SHA256_HEX),
        ):
            v = src.get(key)
            if v is None:
                continue
            if not isinstance(v, str) or not pattern.match(v):
                return False
        return True

    def _validate_metadata(self, meta: dict, output_dir: Path) -> bool:
        """Reject attacker-crafted metadata.json content."""
        pages = meta.get("pages", [])
        if not isinstance(pages, list):
            return False
        if len(pages) > 10_000:  # more pages than any legitimate doc
            return False
        output_real = output_dir.resolve(strict=False)
        for p in pages:
            if not isinstance(p, dict):
                return False
            fname = p.get("file")
            if not isinstance(fname, str) or not self._SAFE_PAGE_FILE.match(fname):
                return False
            # Ensure the file (if claimed to exist) resolves inside output_dir.
            candidate = (output_dir / fname).resolve(strict=False)
            try:
                candidate.relative_to(output_real)
            except ValueError:
                return False
            # Reject derivative descriptors that try to escape too.
            for key in ("trimmed", "focused"):
                sub = p.get(key)
                if sub is None:
                    continue
                if not isinstance(sub, dict):
                    return False
                sub_file = sub.get("file")
                if sub_file is not None:
                    if not isinstance(sub_file, str) or not self._SAFE_PAGE_FILE.match(
                        sub_file
                    ):
                        return False
                    sub_real = (output_dir / sub_file).resolve(strict=False)
                    try:
                        sub_real.relative_to(output_real)
                    except ValueError:
                        return False
                if not self._hash_fields_valid(sub):
                    return False
            if not self._hash_fields_valid(p):
                return False
            if "image_count" in p:
                if not isinstance(p["image_count"], int) or p["image_count"] < 0:
                    return False
            # sheet_name is an opaque label sourced from the input document's
            # PDF outline (for spreadsheets). Cap length and require str.
            if "sheet_name" in p:
                sn = p["sheet_name"]
                if not isinstance(sn, str) or not sn:
                    return False
                if len(sn) > 255:
                    p["sheet_name"] = sn[:255]
        render = meta.get("render", {})
        if not isinstance(render, dict):
            return False
        for key in ("page_count_total", "page_count_rendered"):
            v = render.get(key)
            if v is not None and (
                not isinstance(v, (int, float)) or v < 0 or v > 100_000
            ):
                return False
        for key in ("image_page_count", "total_image_count"):
            v = render.get(key)
            if v is not None and (not isinstance(v, int) or v < 0):
                return False

        # Spreadsheet sheet-inventory block (optional). Worker-sourced, so
        # truncate anything wild and reject obvious shape bugs.
        sheets_block = meta.get("sheets")
        if sheets_block is not None:
            if not isinstance(sheets_block, dict):
                return False
            for key in ("total", "rendered"):
                v = sheets_block.get(key)
                if v is not None and (not isinstance(v, int) or v < 0 or v > 100_000):
                    return False
            non_rendered = sheets_block.get("non_rendered")
            if non_rendered is not None:
                if not isinstance(non_rendered, list) or len(non_rendered) > 10_000:
                    return False
                for entry in non_rendered:
                    if not isinstance(entry, dict):
                        return False
                    name = entry.get("name")
                    if not isinstance(name, str) or not name:
                        return False
                    if len(name) > 255:
                        entry["name"] = name[:255]
                    for key in ("state", "type"):
                        v = entry.get(key)
                        if v is not None and (not isinstance(v, str) or len(v) > 64):
                            return False

        # --- begin T15 additions: scanner fields ---
        scanners = render.get("scanners")
        if not isinstance(scanners, dict):
            return False
        for key in ("qr", "ocr"):
            sub = scanners.get(key)
            if not isinstance(sub, dict):
                return False
            if "enabled" not in sub:
                return False

        MAX_QR_ENTRIES = 1000
        MAX_STRING_CHARS = 64 * 1024
        MAX_OCR_TEXT_CHARS = 1024 * 1024
        for page in meta.get("pages", []):
            if not isinstance(page, dict):
                return False
            qr = page.get("qr")
            if not isinstance(qr, list):
                return False
            if len(qr) > MAX_QR_ENTRIES:
                return False
            for entry in qr:
                if not isinstance(entry, dict):
                    return False
                for field in (
                    "format",
                    "value",
                    "position",
                    "error_correction_level",
                    "raw_bytes_hex",
                ):
                    v = entry.get(field)
                    if v is None:
                        continue
                    if not isinstance(v, str):
                        return False
                    # Truncate oversized string fields rather than reject the
                    # whole metadata — an attacker-crafted QR with a huge
                    # payload shouldn't be able to DoS a whole conversion
                    # (the rendered pages are already on disk). Record a
                    # warning so downstream consumers know.
                    if len(v) > MAX_STRING_CHARS:
                        entry[field] = v[:MAX_STRING_CHARS]
                        meta.setdefault("warnings", []).append(
                            {
                                "code": "qr_field_truncated",
                                "page": page.get("index"),
                                "field": field,
                                "message": f"QR {field} truncated to {MAX_STRING_CHARS} chars",
                            }
                        )

            ocr = page.get("ocr")
            if not isinstance(ocr, dict):
                return False
            for required in ("text", "char_count", "duration_ms"):
                if required not in ocr:
                    return False
            if not isinstance(ocr.get("text"), str):
                return False
            if len(ocr["text"]) > MAX_OCR_TEXT_CHARS:
                ocr["text"] = ocr["text"][:MAX_OCR_TEXT_CHARS]
                ocr["char_count"] = MAX_OCR_TEXT_CHARS
                meta.setdefault("warnings", []).append(
                    {
                        "code": "ocr_text_truncated",
                        "page": page.get("index"),
                        "message": f"OCR text truncated to {MAX_OCR_TEXT_CHARS} chars",
                    }
                )
        # --- end T15 additions ---

        return True

    def _page_counts(self, metadata: dict, output_dir: Path) -> tuple[int, int]:
        """Derive page counts — prefer a filesystem scan over trusting metadata.

        A malicious metadata.json could inflate `page_count_rendered` to
        confuse operators. We authoritatively count actual `page-NNN.png`
        files in `output_dir`, then fall back to metadata's declared
        total (capped at the observed rendered count) for `pages_total`.
        """
        try:
            pages_done = sum(
                1
                for p in output_dir.glob("page-*.png")
                if self._SAFE_PAGE_FILE.match(p.name)
                and "trimmed" not in p.name
                and "focused" not in p.name
            )
        except OSError:
            pages_done = 0
        render = metadata.get("render", {}) if isinstance(metadata, dict) else {}
        declared_total = (
            render.get("page_count_total", pages_done)
            if isinstance(render, dict)
            else pages_done
        )
        try:
            declared_total = int(declared_total)
        except (TypeError, ValueError):
            declared_total = pages_done
        pages_total = max(pages_done, min(declared_total, 100_000))
        return pages_done, pages_total

    def _merge_warnings(self, existing: list[str], new: list[str]) -> list[str]:
        merged: list[str] = []
        for item in [*existing, *new]:
            if item not in merged:
                merged.append(item)
        return merged

    def _format_failure(self, completed: subprocess.CompletedProcess) -> str:
        stderr = self._coerce_text(getattr(completed, "stderr", "")).strip()
        stdout = self._coerce_text(getattr(completed, "stdout", "")).strip()
        detail = stderr or stdout or "no worker output"
        return sanitize_public_error(f"worker exit {completed.returncode}: {detail}")

    def _coerce_text(self, value: object) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        if value is None:
            return ""
        return str(value)

    def _fail_job(self, job: Job, output_dir: Path, error: str) -> None:
        error = sanitize_public_error(error)
        finished = time.time()
        expires = (
            finished + self._job_retention_seconds
            if self._job_retention_seconds > 0
            else None
        )
        self._job_store.update(
            job.job_id,
            status=JobStatus.FAILED,
            finished_at=finished,
            expires_at=expires,
            error=error,
            result_dir=str(output_dir),
            worker_runtime=None,
            security_warnings=list(job.security_warnings),
        )
