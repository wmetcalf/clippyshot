"""Thread-safe in-memory JobStore."""
from __future__ import annotations

import threading
import time

from clippyshot.jobs.base import Job, JobStatus


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def create(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> Job:
        with self._lock:
            job = self._jobs[job_id]
            for k, v in fields.items():
                setattr(job, k, v)
            return job

    def list(self, status: JobStatus | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        return jobs

    def claim_next(self) -> Job | None:
        with self._lock:
            queued = [job for job in self._jobs.values() if job.status == JobStatus.QUEUED]
            if not queued:
                return None
            job = min(queued, key=lambda item: (item.created_at, item.job_id))
            job.status = JobStatus.RUNNING
            job.started_at = time.time()
            return job

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
