"""SQL-backed JobStore with SQLite and Postgres URL support."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from clippyshot.jobs.base import Job, JobStatus


_COLUMNS = (
    "job_id",
    "filename",
    "status",
    "created_at",
    "started_at",
    "finished_at",
    "pages_done",
    "pages_total",
    "error",
    "result_dir",
    "worker_runtime",
    "security_warnings",
    "detected",
    "scan_options",
    "expires_at",
)


class SqlJobStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._lock = threading.RLock()
        self._driver, self._param = self._parse_url(database_url)
        self._init_db()

    def _parse_url(self, database_url: str) -> tuple[str, str]:
        scheme = urlparse(database_url).scheme.lower()
        if scheme == "sqlite":
            return "sqlite", "?"
        if scheme in {"postgres", "postgresql"}:
            return "postgres", "%s"
        raise ValueError(f"unsupported database url: {database_url}")

    @contextmanager
    def _connect(self):
        if self._driver == "sqlite":
            parsed = urlparse(self._database_url)
            db_path = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc != "localhost":
                db_path = f"//{parsed.netloc}{db_path}"
            if not db_path:
                raise ValueError("sqlite database url requires a path")
            if not db_path.startswith("/"):
                db_path = str(Path.cwd() / db_path)
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        else:
            import psycopg

            conn = psycopg.connect(self._database_url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            started_at DOUBLE PRECISION,
            finished_at DOUBLE PRECISION,
            pages_done INTEGER NOT NULL DEFAULT 0,
            pages_total INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            result_dir TEXT,
            worker_runtime TEXT,
            security_warnings TEXT,
            detected TEXT,
            scan_options TEXT,
            expires_at DOUBLE PRECISION
        )
        """
        with self._lock, self._connect() as conn:
            conn.execute(sql)
            self._ensure_columns(conn)

    def _ensure_columns(self, conn) -> None:
        existing = self._existing_columns(conn)
        if "scan_options" in existing:
            return
        conn.execute(f"ALTER TABLE jobs ADD COLUMN scan_options TEXT")

    def _existing_columns(self, conn) -> set[str]:
        if self._driver == "sqlite":
            rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
            return {str(row[1]) for row in rows}
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'jobs'
            """
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _encode_value(self, key: str, value):
        if (
            key in {"detected", "security_warnings", "scan_options"}
            and value is not None
        ):
            return json.dumps(value)
        return value

    def _row_to_job(self, row) -> Job | None:
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            raw = {k: row[k] for k in row.keys()}
        else:
            raw = dict(zip(_COLUMNS, row, strict=True))
        if raw.get("detected"):
            raw["detected"] = json.loads(raw["detected"])
        if raw.get("security_warnings"):
            raw["security_warnings"] = json.loads(raw["security_warnings"])
        elif raw.get("security_warnings") is None:
            raw["security_warnings"] = []
        if raw.get("scan_options"):
            raw["scan_options"] = json.loads(raw["scan_options"])
        return Job.from_dict(raw)

    def create(self, job: Job) -> None:
        values = job.to_dict()
        cols = ", ".join(_COLUMNS)
        params = ", ".join(self._param for _ in _COLUMNS)
        sql = f"INSERT INTO jobs ({cols}) VALUES ({params})"
        with self._lock, self._connect() as conn:
            conn.execute(
                sql, tuple(self._encode_value(col, values.get(col)) for col in _COLUMNS)
            )

    def get(self, job_id: str) -> Job | None:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM jobs WHERE job_id = {self._param}"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (job_id,)).fetchone()
        return self._row_to_job(row)

    def update(self, job_id: str, **fields) -> Job:
        if not fields:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return job
        encoded = {key: self._encode_value(key, value) for key, value in fields.items()}
        set_clause = ", ".join(f"{key} = {self._param}" for key in encoded)
        sql = f"UPDATE jobs SET {set_clause} WHERE job_id = {self._param}"
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, tuple(encoded.values()) + (job_id,))
            if cur.rowcount == 0:
                raise KeyError(job_id)
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list(self, status: JobStatus | None = None) -> list[Job]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM jobs"
        params: tuple = ()
        if status is not None:
            sql += f" WHERE status = {self._param}"
            params = (status.value,)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [job for row in rows if (job := self._row_to_job(row)) is not None]

    def claim_next(self) -> Job | None:
        if self._driver == "sqlite":
            return self._claim_next_sqlite()
        return self._claim_next_postgres()

    def _claim_next_sqlite(self) -> Job | None:
        select_sql = (
            f"SELECT {', '.join(_COLUMNS)} FROM jobs "
            f"WHERE status = {self._param} "
            f"ORDER BY created_at ASC, job_id ASC LIMIT 1"
        )
        update_sql = (
            f"UPDATE jobs SET status = {self._param}, started_at = {self._param} "
            f"WHERE job_id = {self._param}"
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(select_sql, (JobStatus.QUEUED.value,)).fetchone()
            if row is None:
                return None
            job = self._row_to_job(row)
            if job is None:
                return None
            started_at = time.time()
            conn.execute(update_sql, (JobStatus.RUNNING.value, started_at, job.job_id))
            job.status = JobStatus.RUNNING
            job.started_at = started_at
            return job

    def _claim_next_postgres(self) -> Job | None:
        sql = f"""
        WITH next_job AS (
            SELECT job_id
            FROM jobs
            WHERE status = {self._param}
            ORDER BY created_at ASC, job_id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE jobs
        SET status = {self._param}, started_at = {self._param}
        FROM next_job
        WHERE jobs.job_id = next_job.job_id
        RETURNING {', '.join('jobs.' + col for col in _COLUMNS)}
        """
        params = (JobStatus.QUEUED.value, JobStatus.RUNNING.value, time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return self._row_to_job(row)

    def delete(self, job_id: str) -> None:
        sql = f"DELETE FROM jobs WHERE job_id = {self._param}"
        with self._lock, self._connect() as conn:
            conn.execute(sql, (job_id,))
