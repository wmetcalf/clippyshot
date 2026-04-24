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
    "input_sha256",
)


class SqlJobStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._lock = threading.RLock()
        self._driver, self._param = self._parse_url(database_url)
        self._pool = None
        if self._driver == "postgres":
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._database_url, min_size=1, max_size=8, timeout=10.0
            )
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
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        else:
            with self._pool.connection() as conn:
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

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
            expires_at DOUBLE PRECISION,
            input_sha256 TEXT
        )
        """
        # Per-page perceptual-hash index for similarity search. phash is
        # stored as signed int8 so the pg_bktree SP-GiST operator class
        # (bktree_ops) can index it for fast Hamming-distance queries.
        # colorhash and sha256 are exact-match lookups via btree.
        #
        # `BIGINT` on PostgreSQL == `int8` == int64. SQLite treats it as
        # INTEGER, so the same DDL works on both backends (queries that
        # use bktree_area / hamming_distance only run on Postgres — the
        # SQLite dev path falls back to a sequential scan).
        # One row per (page, variant). Variants are 'original' (the raw
        # rasterizer output), 'trimmed' (solid-bottom crop, when present),
        # and 'focused' (dense-content crop, when present). Indexing all
        # three lets similarity search match against "the logo crop of
        # this doc" or "the header-trimmed view" independently of the
        # full render.
        page_hashes_sql = """
        CREATE TABLE IF NOT EXISTS page_hashes (
            job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            page_index INTEGER NOT NULL,
            variant TEXT NOT NULL DEFAULT 'original',
            phash BIGINT NOT NULL,
            colorhash TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (job_id, page_index, variant)
        )
        """
        with self._lock, self._connect() as conn:
            conn.execute(sql)
            conn.execute(page_hashes_sql)
            self._ensure_columns(conn)
            self._ensure_page_hash_indexes(conn)

    def _ensure_columns(self, conn) -> None:
        existing = self._existing_columns(conn)
        for col in ("scan_options", "input_sha256"):
            if col not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT")
        # page_hashes.variant migration: older DBs have (job_id, page_index)
        # as the PK and no variant column. Add the column + promote the PK
        # to include variant so upserts can store per-variant rows.
        ph_cols = self._existing_columns_for(conn, "page_hashes")
        if ph_cols and "variant" not in ph_cols:
            if self._driver == "postgres":
                conn.execute(
                    "ALTER TABLE page_hashes "
                    "ADD COLUMN variant TEXT NOT NULL DEFAULT 'original'"
                )
                try:
                    conn.execute(
                        "ALTER TABLE page_hashes DROP CONSTRAINT page_hashes_pkey"
                    )
                except Exception:
                    pass  # pkey already dropped by a previous migration run
                conn.execute(
                    "ALTER TABLE page_hashes "
                    "ADD PRIMARY KEY (job_id, page_index, variant)"
                )
            else:
                # SQLite: ALTER PRIMARY KEY isn't supported directly. Add
                # the column only; existing dev installs keep the old PK
                # (one row per page), so only the last-inserted variant
                # persists. Acceptable for the dev path.
                conn.execute(
                    "ALTER TABLE page_hashes "
                    "ADD COLUMN variant TEXT NOT NULL DEFAULT 'original'"
                )

    def _ensure_page_hash_indexes(self, conn) -> None:
        """Create the page_hashes indexes if the backend supports them.

        - btree on (colorhash), (sha256), (phash) — always safe.
        - SP-GiST on (phash bktree_ops) — Postgres + pg_bktree only.
          Silently skipped on SQLite and on any Postgres without the
          extension installed, so dev installs still work.
        """
        # Exact-match btrees — portable across sqlite + postgres.
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_colorhash ON page_hashes (colorhash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_sha256 ON page_hashes (sha256)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_phash ON page_hashes (phash)"
            )
        except Exception:
            pass  # Dev install may lack permissions; not fatal.
        if self._driver != "postgres":
            return
        # Detect bktree extension. When absent, drop back to the btree
        # phash index (already created above); the API layer falls back
        # to a sequential-scan Hamming query.
        try:
            row = conn.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'bktree' LIMIT 1"
            ).fetchone()
        except Exception:
            return
        if not row:
            return
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_phash_bktree "
                "ON page_hashes USING spgist (phash bktree_ops)"
            )
        except Exception:
            pass  # Index creation races are non-fatal; queries still work.
        # colorhash_bin_distance is installed by the container's initdb
        # script, but dev DBs and pre-existing installs won't have it, so
        # we install it on the fly. IMMUTABLE/PARALLEL SAFE so re-running
        # CREATE OR REPLACE is cheap and idempotent.
        try:
            conn.execute(
                "CREATE OR REPLACE FUNCTION colorhash_bin_distance("
                "a text, b text, first_bin int DEFAULT 0, last_bin int DEFAULT 14"
                ") RETURNS int AS $$ "
                "SELECT CASE "
                "WHEN length(a) <> 14 OR length(b) <> 14 THEN 2147483647 "
                "ELSE coalesce(("
                "SELECT sum(abs("
                "('x' || substring(a FROM i+1 FOR 1))::bit(4)::int "
                "- ('x' || substring(b FROM i+1 FOR 1))::bit(4)::int"
                "))::int "
                "FROM generate_series(first_bin, last_bin - 1) AS i"
                "), 0) END "
                "$$ LANGUAGE SQL IMMUTABLE PARALLEL SAFE"
            )
        except Exception:
            pass  # Function creation races / permission errors are non-fatal.

    def _existing_columns(self, conn) -> set[str]:
        return self._existing_columns_for(conn, "jobs")

    def _existing_columns_for(self, conn, table: str) -> set[str]:
        if self._driver == "sqlite":
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {str(row[1]) for row in rows}
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s",
            (table,),
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
        for key in fields:
            if key not in _COLUMNS:
                raise ValueError(f"invalid column: {key}")
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

    # ---- page_hashes ---------------------------------------------------

    def upsert_page_hashes(self, job_id: str, rows: list[dict]) -> None:
        """Write a batch of per-page hashes for a completed job.

        ``rows`` is a list of dicts with keys: page_index, variant
        ('original' / 'trimmed' / 'focused'; defaults to 'original' for
        backward-compat), phash (signed int8), colorhash (hex),
        sha256 (hex). Dispatcher calls this once per job, right after
        validating metadata.json. Silently no-ops if rows is empty.
        """
        if not rows:
            return
        now = time.time()
        if self._driver == "sqlite":
            sql = (
                "INSERT OR REPLACE INTO page_hashes "
                "(job_id, page_index, variant, phash, colorhash, sha256, created_at) "
                f"VALUES ({self._param}, {self._param}, {self._param}, "
                f"{self._param}, {self._param}, {self._param}, {self._param})"
            )
        else:
            sql = (
                "INSERT INTO page_hashes "
                "(job_id, page_index, variant, phash, colorhash, sha256, created_at) "
                f"VALUES ({self._param}, {self._param}, {self._param}, "
                f"{self._param}, {self._param}, {self._param}, {self._param}) "
                "ON CONFLICT (job_id, page_index, variant) DO UPDATE SET "
                "phash = EXCLUDED.phash, "
                "colorhash = EXCLUDED.colorhash, "
                "sha256 = EXCLUDED.sha256, "
                "created_at = EXCLUDED.created_at"
            )
        with self._lock, self._connect() as conn:
            params = [
                (
                    job_id,
                    int(r["page_index"]),
                    str(r.get("variant", "original")),
                    int(r["phash"]),
                    str(r["colorhash"]),
                    str(r["sha256"]),
                    now,
                )
                for r in rows
            ]
            conn.executemany(sql, params)

    def find_similar_phash(
        self, target_int8: int, max_distance: int, limit: int = 50
    ) -> list[dict]:
        """Return pages within Hamming distance ``max_distance`` of target.

        Uses pg_bktree's SP-GiST operator when the extension is present
        (sub-millisecond at 100k+ rows), falls back to a sequential scan
        with ``hamming_distance`` function on Postgres without the
        extension, and a Python-level popcount on SQLite.
        """
        if self._driver == "sqlite":
            # SQLite: in-memory popcount.
            rows = self._query_all(
                "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
                "ph.sha256, j.filename "
                "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
                f"WHERE j.status = {self._param}",
                (JobStatus.DONE.value,),
            )
            out = []
            for r in rows:
                dist = bin((r["phash"] ^ target_int8) & ((1 << 64) - 1)).count("1")
                if dist <= max_distance:
                    out.append({**r, "distance": dist})
            out.sort(key=lambda x: (x["distance"], x["job_id"], x["page_index"]))
            return out[:limit]
        # Postgres — prefer the BK-tree range scan.
        bktree_available = self._bktree_extension_available()
        if bktree_available:
            sql = (
                "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
                "ph.sha256, j.filename, "
                f"hamming_distance(ph.phash, {self._param}::int8) AS distance "
                "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
                f"WHERE j.status = {self._param} "
                f"AND ph.phash <@ ROW({self._param}::int8, {self._param}::int8)::bktree_area "
                f"ORDER BY distance, ph.job_id, ph.page_index LIMIT {self._param}"
            )
            params = (
                target_int8,
                JobStatus.DONE.value,
                target_int8,
                max_distance,
                limit,
            )
        else:
            sql = (
                "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
                "ph.sha256, j.filename, "
                f"bit_count((ph.phash # {self._param}::int8)::bit(64)) AS distance "
                "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
                f"WHERE j.status = {self._param} "
                f"AND bit_count((ph.phash # {self._param}::int8)::bit(64)) <= {self._param} "
                f"ORDER BY distance, ph.job_id, ph.page_index LIMIT {self._param}"
            )
            params = (
                target_int8,
                JobStatus.DONE.value,
                target_int8,
                max_distance,
                limit,
            )
        return self._query_all(sql, params)

    def find_by_colorhash(self, colorhash: str, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
            "ph.sha256, j.filename "
            "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
            f"WHERE j.status = {self._param} AND ph.colorhash = {self._param} "
            f"ORDER BY ph.job_id, ph.page_index LIMIT {self._param}"
        )
        return self._query_all(sql, (JobStatus.DONE.value, colorhash, limit))

    def find_by_page_sha256(self, sha256: str, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
            "ph.sha256, j.filename "
            "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
            f"WHERE j.status = {self._param} AND ph.sha256 = {self._param} "
            f"ORDER BY ph.job_id, ph.page_index LIMIT {self._param}"
        )
        return self._query_all(sql, (JobStatus.DONE.value, sha256, limit))

    def find_similar_colorhash(
        self,
        target: str,
        *,
        total_max: int | None = None,
        frac_max: int | None = None,
        faint_max: int | None = None,
        bright_max: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return pages whose colorhash falls within the given bin distances.

        imagehash's colorhash encodes 14 4-bit bins in hex (binbits=4):
            0–1:   fraction bins (black, gray)
            2–7:   6 faint-color hue bins
            8–13:  6 bright-color hue bins
        We compute the L1 distance per nibble. Callers can cap the total
        (simple mode) and/or per-group distances (advanced mode). Passing no
        caps falls back to an exact-match lookup.
        """
        if not any(v is not None for v in (total_max, frac_max, faint_max, bright_max)):
            return self.find_by_colorhash(target, limit=limit)
        groups = [
            (total_max, 0, 14, "distance"),
            (frac_max, 0, 2, "frac_distance"),
            (faint_max, 2, 8, "faint_distance"),
            (bright_max, 8, 14, "bright_distance"),
        ]
        if self._driver == "sqlite":
            rows = self._query_all(
                "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
                "ph.sha256, j.filename "
                "FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
                f"WHERE j.status = {self._param}",
                (JobStatus.DONE.value,),
            )
            out: list[dict] = []
            for r in rows:
                ch = r.get("colorhash") or ""
                if len(ch) != 14 or len(target) != 14:
                    continue
                dists = {}
                keep = True
                for cap, first, last, alias in groups:
                    d = sum(
                        abs(int(ch[i], 16) - int(target[i], 16))
                        for i in range(first, last)
                    )
                    dists[alias] = d
                    if cap is not None and d > cap:
                        keep = False
                        break
                if keep:
                    out.append({**r, **dists})
            out.sort(key=lambda x: (x["distance"], x["job_id"], x["page_index"]))
            return out[:limit]
        # Postgres: per-group SQL function call. No index viable for L1
        # distance, so this is a seq scan over page_hashes — fine at our
        # scale (a few tens of thousands of rows).
        # IMPORTANT: SELECT placeholders must come before WHERE placeholders
        # in ``params`` to match the order they appear in the final SQL;
        # build two lists and concatenate at the end rather than pushing as
        # we go.
        select_cols: list[str] = []
        select_params: list = []
        where_clauses: list[str] = []
        where_params: list = []
        for cap, first, last, alias in groups:
            select_cols.append(
                f"colorhash_bin_distance(ph.colorhash, {self._param}, {first}, {last}) AS {alias}"
            )
            select_params.append(target)
            if cap is not None:
                where_clauses.append(
                    f"colorhash_bin_distance(ph.colorhash, {self._param}, {first}, {last}) <= {self._param}"
                )
                where_params.extend([target, cap])
        sql = (
            "SELECT ph.job_id, ph.page_index, ph.variant, ph.phash, ph.colorhash, "
            "ph.sha256, j.filename, "
            + ", ".join(select_cols)
            + " FROM page_hashes ph JOIN jobs j ON j.job_id = ph.job_id "
            + f"WHERE j.status = {self._param} "
            + (f"AND {' AND '.join(where_clauses)} " if where_clauses else "")
            + f"ORDER BY distance, ph.job_id, ph.page_index LIMIT {self._param}"
        )
        params = select_params + [JobStatus.DONE.value] + where_params + [limit]
        return self._query_all(sql, tuple(params))

    def _bktree_extension_available(self) -> bool:
        with self._lock, self._connect() as conn:
            try:
                row = conn.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'bktree' LIMIT 1"
                ).fetchone()
            except Exception:
                return False
            return bool(row)

    def _query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a SELECT and return rows as list-of-dicts, portable across drivers."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            if self._driver == "sqlite":
                return [dict(r) for r in rows]
            # psycopg returns tuples; use cursor.description for keys.
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in rows]
