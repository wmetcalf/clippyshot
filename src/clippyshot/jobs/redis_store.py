"""Redis-backed JobStore."""
from __future__ import annotations

import json
import time
from redis.exceptions import WatchError

from clippyshot.jobs.base import Job, JobStatus


_PREFIX = "clippyshot:job:"
_TTL_SECONDS = 60 * 60 * 24


class RedisJobStore:
    def __init__(self, client, *, ttl_seconds: int = _TTL_SECONDS) -> None:
        self._r = client
        self._ttl_seconds = ttl_seconds

    def _key(self, job_id: str) -> str:
        return _PREFIX + job_id

    def create(self, job: Job) -> None:
        self._r.set(
            self._key(job.job_id),
            json.dumps(job.to_dict()),
            ex=self._ttl_seconds,
        )

    def get(self, job_id: str) -> Job | None:
        raw = self._r.get(self._key(job_id))
        if raw is None:
            return None
        return Job.from_dict(json.loads(raw))

    def update(self, job_id: str, **fields) -> Job:
        """Atomic read-modify-write using WATCH/MULTI/EXEC."""
        key = self._key(job_id)
        with self._r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if raw is None:
                        raise KeyError(job_id)
                    job = Job.from_dict(json.loads(raw))
                    for k, v in fields.items():
                        setattr(job, k, v)
                    pipe.multi()
                    pipe.set(key, json.dumps(job.to_dict()), ex=self._ttl_seconds)
                    pipe.execute()
                    return job
                except WatchError:
                    continue  # retry on concurrent modification

    def list(self, status: JobStatus | None = None) -> list[Job]:
        jobs: list[Job] = []
        for k in self._r.scan_iter(match=_PREFIX + "*", count=200):
            raw = self._r.get(k)
            if raw is None:
                continue
            job = Job.from_dict(json.loads(raw))
            if status is None or job.status == status:
                jobs.append(job)
        return jobs

    def claim_next(self) -> Job | None:
        while True:
            candidates: list[tuple[float, str, str]] = []
            for k in self._r.scan_iter(match=_PREFIX + "*", count=200):
                raw = self._r.get(k)
                if raw is None:
                    continue
                job = Job.from_dict(json.loads(raw))
                if job.status == JobStatus.QUEUED:
                    candidates.append((job.created_at, job.job_id, k))
            if not candidates:
                return None

            _, _, key = min(candidates, key=lambda item: (item[0], item[1]))
            try:
                with self._r.pipeline() as pipe:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if raw is None:
                        continue
                    job = Job.from_dict(json.loads(raw))
                    if job.status != JobStatus.QUEUED:
                        continue
                    job.status = JobStatus.RUNNING
                    job.started_at = time.time()
                    pipe.multi()
                    pipe.set(key, json.dumps(job.to_dict()), ex=self._ttl_seconds)
                    pipe.execute()
                    return job
            except WatchError:
                continue

    def delete(self, job_id: str) -> None:
        self._r.delete(self._key(job_id))
