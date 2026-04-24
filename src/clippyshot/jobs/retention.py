"""Retention helpers for async job artifacts."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from clippyshot.jobs.base import JobStatus, JobStore

_log = logging.getLogger("clippyshot.jobs.retention")


class JobArtifactRegistry:
    """Thread-safe registry of job output directories with TTL-based expiry.

    ``mark_finished`` is called from thread-pool workers (via ``run_in_executor``),
    while ``expire_due`` runs on the event loop or in the background sweeper.
    A lock serialises mutations to prevent races between concurrent updates and
    the snapshot-then-pop pattern in ``expire_due``.
    """

    def __init__(
        self,
        *,
        retention_seconds: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._retention_seconds = max(0, retention_seconds)
        self._clock = clock or time.time
        self._artifacts: dict[str, tuple[Path, float | None]] = {}
        self._in_use: dict[str, int] = {}
        self._lock = threading.Lock()

    def register(self, job_id: str, output_dir: Path) -> None:
        with self._lock:
            self._artifacts[job_id] = (Path(output_dir), None)

    def mark_finished(self, job_id: str) -> None:
        with self._lock:
            entry = self._artifacts.get(job_id)
            if entry is None:
                return
            out, _ = entry
            expires_at = None
            if self._retention_seconds > 0:
                expires_at = self._clock() + self._retention_seconds
            self._artifacts[job_id] = (out, expires_at)

    def acquire(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._artifacts:
                return False
            self._in_use[job_id] = self._in_use.get(job_id, 0) + 1
            return True

    def release(self, job_id: str) -> None:
        with self._lock:
            count = self._in_use.get(job_id)
            if count is None:
                return
            if count <= 1:
                self._in_use.pop(job_id, None)
                return
            self._in_use[job_id] = count - 1

    def in_use(self, job_id: str) -> bool:
        with self._lock:
            return self._in_use.get(job_id, 0) > 0

    def path_for(self, job_id: str) -> Path | None:
        with self._lock:
            entry = self._artifacts.get(job_id)
            return None if entry is None else entry[0]

    def delete(self, job_id: str) -> None:
        with self._lock:
            if self._in_use.get(job_id, 0) > 0:
                return
            entry = self._artifacts.pop(job_id, None)
        if entry is None:
            return
        out, _ = entry
        shutil.rmtree(out.parent, ignore_errors=True)

    def expire_due(self, job_store: JobStore) -> list[str]:
        """Expire finished jobs past their retention TTL.

        Each job is processed independently — a failure on one job (e.g. a
        transient Redis timeout) logs a warning and continues to the next.
        This prevents a single bad entry from crashing the background sweeper.
        """
        now = self._clock()
        expired: list[str] = []
        with self._lock:
            snapshot = list(self._artifacts.items())
        tracked_ids = {job_id for job_id, _ in snapshot}
        for job_id, (out, expires_at) in snapshot:
            if expires_at is None or expires_at > now:
                continue
            if self.in_use(job_id):
                continue
            try:
                self._expire_job(job_store, job_id, out)
                expired.append(job_id)
            except Exception:
                _log.exception("failed to expire job %s", job_id)

        for job in job_store.list():
            if job.job_id in tracked_ids:
                continue
            if job.expires_at is None or job.expires_at > now:
                continue
            if self.in_use(job.job_id):
                continue
            # Expire any terminal OR long-stale job. Orphaned queued/running
            # jobs (e.g. dispatcher died mid-flight) also need cleanup so
            # their input dirs don't accumulate.
            out_path: Path | None = None
            if job.result_dir is not None:
                out_path = Path(job.result_dir)
            try:
                if out_path is not None:
                    self._expire_job(job_store, job.job_id, out_path)
                else:
                    # No result_dir on file — just mark expired so the
                    # job row/key goes away.
                    job_store.update(job.job_id, status=JobStatus.EXPIRED)
                expired.append(job.job_id)
            except Exception:
                _log.exception("failed to expire persisted job %s", job.job_id)
        return expired

    def _expire_job(self, job_store: JobStore, job_id: str, out: Path) -> None:
        shutil.rmtree(out.parent, ignore_errors=True)
        with self._lock:
            self._artifacts.pop(job_id, None)
        job = job_store.get(job_id)
        if job is not None:
            job_store.update(
                job_id,
                status=JobStatus.EXPIRED,
                result_dir=None,
            )
