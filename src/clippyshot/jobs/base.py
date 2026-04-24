"""Job model and JobStore protocol."""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from clippyshot.errors import sanitize_public_error


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class Job:
    job_id: str
    filename: str
    status: JobStatus
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    pages_done: int = 0
    pages_total: int = 0
    error: str | None = None
    result_dir: str | None = None
    worker_runtime: str | None = None
    security_warnings: list[str] = field(default_factory=list)
    detected: dict | None = None  # detection info (magika, libmagic, label)
    scan_options: dict[str, str] | None = None
    expires_at: float | None = None
    input_sha256: str | None = None  # SHA-256 of the uploaded bytes

    @classmethod
    def new(cls, filename: str) -> "Job":
        return cls(
            job_id=str(uuid.uuid4()),
            filename=filename,
            status=JobStatus.QUEUED,
            created_at=time.time(),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def to_public_dict(self) -> dict:
        d = self.to_dict()
        d.pop("result_dir", None)
        d.pop("scan_options", None)
        if isinstance(d.get("error"), str):
            d["error"] = sanitize_public_error(d["error"])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            job_id=d["job_id"],
            filename=d["filename"],
            status=JobStatus(d["status"]),
            created_at=float(d["created_at"]),
            started_at=float(d["started_at"]) if d.get("started_at") else None,
            finished_at=float(d["finished_at"]) if d.get("finished_at") else None,
            pages_done=int(d.get("pages_done", 0)),
            pages_total=int(d.get("pages_total", 0)),
            error=d.get("error"),
            result_dir=d.get("result_dir"),
            worker_runtime=d.get("worker_runtime"),
            security_warnings=(
                list(d.get("security_warnings", []))
                if d.get("security_warnings") is not None
                else []
            ),
            detected=d.get("detected"),
            scan_options=d.get("scan_options"),
            expires_at=float(d["expires_at"]) if d.get("expires_at") else None,
            input_sha256=d.get("input_sha256"),
        )


@runtime_checkable
class JobStore(Protocol):
    def create(self, job: Job) -> None: ...
    def get(self, job_id: str) -> Job | None: ...
    def update(self, job_id: str, **fields) -> Job: ...
    def list(self, status: JobStatus | None = None) -> list[Job]: ...
    def claim_next(self) -> Job | None: ...
    def delete(self, job_id: str) -> None: ...
