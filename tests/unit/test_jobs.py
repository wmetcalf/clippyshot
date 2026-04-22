from pathlib import Path

import pytest
from redis.exceptions import WatchError

from clippyshot.jobs import InMemoryJobStore, Job, JobStatus, RedisJobStore, SqlJobStore

try:
    import fakeredis
except ImportError:  # pragma: no cover - depends on local test env
    fakeredis = None


@pytest.fixture(params=["memory", "redis", "sql"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        yield InMemoryJobStore()
    elif request.param == "redis":
        if fakeredis is None:
            pytest.skip("fakeredis not installed")
        yield RedisJobStore(client=fakeredis.FakeStrictRedis())
    else:
        yield SqlJobStore(f"sqlite:///{tmp_path / 'jobs.db'}")


def test_create_and_get(store):
    job = Job.new(filename="a.docx")
    store.create(job)
    fetched = store.get(job.job_id)
    assert fetched is not None
    assert fetched.filename == "a.docx"
    assert fetched.status == JobStatus.QUEUED


def test_get_returns_none_for_missing(store):
    assert store.get("does-not-exist") is None


def test_update_changes_fields(store):
    job = Job.new(filename="a.docx")
    store.create(job)
    store.update(job.job_id, status=JobStatus.RUNNING, pages_done=2, pages_total=5)
    fetched = store.get(job.job_id)
    assert fetched.status == JobStatus.RUNNING
    assert fetched.pages_done == 2
    assert fetched.pages_total == 5


def test_list_filters_by_status(store):
    a = Job.new(filename="a.docx")
    b = Job.new(filename="b.docx")
    store.create(a)
    store.create(b)
    store.update(b.job_id, status=JobStatus.DONE)

    queued = store.list(status=JobStatus.QUEUED)
    done = store.list(status=JobStatus.DONE)
    assert {j.job_id for j in queued} == {a.job_id}
    assert {j.job_id for j in done} == {b.job_id}


def test_list_without_filter_returns_all(store):
    a = Job.new(filename="a.docx")
    b = Job.new(filename="b.docx")
    store.create(a)
    store.create(b)
    all_jobs = store.list()
    assert {j.job_id for j in all_jobs} == {a.job_id, b.job_id}


def test_delete_removes_job(store):
    job = Job.new(filename="a.docx")
    store.create(job)
    store.delete(job.job_id)
    assert store.get(job.job_id) is None


def test_delete_idempotent(store):
    """Deleting a non-existent job should not raise."""
    store.delete("does-not-exist")  # should not raise


def test_job_to_dict_and_from_dict_roundtrip():
    job = Job.new(filename="a.docx")
    job.pages_done = 3
    job.pages_total = 10
    job.expires_at = 1234.5
    d = job.to_dict()
    restored = Job.from_dict(d)
    assert restored.job_id == job.job_id
    assert restored.filename == job.filename
    assert restored.status == job.status
    assert restored.pages_done == 3
    assert restored.pages_total == 10
    assert restored.expires_at == 1234.5


def test_job_to_public_dict_redacts_result_dir():
    job = Job.new(filename="a.docx")
    job.result_dir = "/tmp/clippyshot-job-123/out"

    public = job.to_public_dict()

    assert public["job_id"] == job.job_id
    assert "result_dir" not in public


def test_job_to_dict_roundtrips_dispatcher_fields():
    job = Job.new(filename="a.docx")
    job.worker_runtime = "runsc"
    job.security_warnings = ["runsc unavailable; fell back to runc"]
    job.scan_options = {"CLIPPYSHOT_ENABLE_OCR": "1"}

    restored = Job.from_dict(job.to_dict())
    public = job.to_public_dict()

    assert restored.worker_runtime == "runsc"
    assert restored.security_warnings == ["runsc unavailable; fell back to runc"]
    assert restored.scan_options == {"CLIPPYSHOT_ENABLE_OCR": "1"}
    assert public["worker_runtime"] == "runsc"
    assert public["security_warnings"] == ["runsc unavailable; fell back to runc"]
    assert "result_dir" not in public
    assert "scan_options" not in public


def test_sql_store_persists_jobs_across_instances(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path / 'jobs.db'}"
    store = SqlJobStore(db_url)
    job = Job.new(filename="persist.docx")
    store.create(job)
    store.update(
        job.job_id,
        status=JobStatus.DONE,
        result_dir="/tmp/out",
        worker_runtime="runsc",
        security_warnings=["fallback runtime available"],
        scan_options={"CLIPPYSHOT_ENABLE_QR": "0"},
        pages_done=2,
        pages_total=2,
        expires_at=321.0,
    )

    reopened = SqlJobStore(db_url)
    fetched = reopened.get(job.job_id)

    assert fetched is not None
    assert fetched.status == JobStatus.DONE
    assert fetched.result_dir == "/tmp/out"
    assert fetched.worker_runtime == "runsc"
    assert fetched.security_warnings == ["fallback runtime available"]
    assert fetched.scan_options == {"CLIPPYSHOT_ENABLE_QR": "0"}
    assert fetched.pages_done == 2
    assert fetched.expires_at == 321.0


@pytest.mark.parametrize("store_name", ["memory", "redis", "sql"])
def test_claim_next_claims_oldest_queued_job_once(store_name, tmp_path: Path):
    if store_name == "memory":
        store = InMemoryJobStore()
    elif store_name == "redis":
        if fakeredis is None:
            pytest.skip("fakeredis not installed")
        store = RedisJobStore(client=fakeredis.FakeStrictRedis())
    else:
        store = SqlJobStore(f"sqlite:///{tmp_path / 'jobs.db'}")

    older = Job.new(filename="older.docx")
    newer = Job.new(filename="newer.docx")
    older.created_at = 100.0
    newer.created_at = 200.0
    store.create(newer)
    store.create(older)

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.job_id == older.job_id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.started_at is not None
    assert store.get(older.job_id).status == JobStatus.RUNNING

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.job_id == newer.job_id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.started_at is not None

    assert store.claim_next() is None


def test_redis_claim_next_uses_a_fresh_pipeline_after_early_continue():
    class FakePipeline:
        def __init__(self, client):
            self.client = client
            self.watched = False
            self.claim_attempts = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def watch(self, key):
            if self.watched:
                raise AssertionError("pipeline state leaked across retry")
            self.watched = True
            self.key = key

        def get(self, key):
            self.client.claim_attempts += 1
            if self.client.claim_attempts == 1:
                return None
            return self.client.get(key)

        def multi(self):
            return None

        def set(self, key, value, ex=None):
            self.pending = (key, value, ex)

        def execute(self):
            self.client.exec_calls += 1
            key, value, ex = self.pending
            self.client.set(key, value, ex=ex)
            return True

    class FakeRedis:
        def __init__(self):
            self.data = {}
            self.exec_calls = 0
            self.pipeline_calls = 0
            self.claim_attempts = 0

        def set(self, key, value, ex=None):
            self.data[key] = value

        def get(self, key):
            return self.data.get(key)

        def scan_iter(self, match=None, count=None):
            return iter(self.data.keys())

        def pipeline(self):
            self.pipeline_calls += 1
            return FakePipeline(self)

    fake = FakeRedis()
    store = RedisJobStore(client=fake)
    job = Job.new(filename="race.docx")
    store.create(job)

    claimed = store.claim_next()

    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == JobStatus.RUNNING
    assert fake.pipeline_calls == 2
    assert fake.exec_calls == 1


def test_redis_update_retries_after_watch_conflict():
    class FakePipeline:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def watch(self, key):
            self.key = key

        def get(self, key):
            return self.client.get(key)

        def multi(self):
            return None

        def set(self, key, value, ex=None):
            self.pending = (key, value, ex)

        def execute(self):
            self.client.exec_calls += 1
            if self.client.exec_calls == 1:
                raise WatchError("simulated concurrent write")
            key, value, ex = self.pending
            self.client.set(key, value, ex=ex)
            return True

    class FakeRedis:
        def __init__(self):
            self.data = {}
            self.exec_calls = 0

        def set(self, key, value, ex=None):
            self.data[key] = value

        def get(self, key):
            return self.data.get(key)

        def pipeline(self):
            return FakePipeline(self)

    fake = FakeRedis()
    store = RedisJobStore(client=fake)
    job = Job.new(filename="race.docx")
    store.create(job)

    updated = store.update(job.job_id, status=JobStatus.RUNNING)

    assert updated.status == JobStatus.RUNNING
    assert store.get(job.job_id).status == JobStatus.RUNNING
    assert fake.exec_calls == 2


def test_sql_store_claim_next_postgres_branch_uses_skip_locked(monkeypatch):
    class FakeResult:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeConn:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, params))
            return FakeResult(
                (
                    "job-1",
                    "claim.docx",
                    JobStatus.RUNNING.value,
                    100.0,
                    200.0,
                    None,
                    0,
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    fake_conn = FakeConn()

    class FakeCtx:
        def __enter__(self):
            return fake_conn

        def __exit__(self, exc_type, exc, tb):
            return False

    store = SqlJobStore.__new__(SqlJobStore)
    store._lock = __import__("threading").RLock()
    store._driver = "postgres"
    store._param = "%s"
    monkeypatch.setattr(store, "_connect", lambda: FakeCtx())

    claimed = store._claim_next_postgres()

    assert claimed is not None
    assert claimed.job_id == "job-1"
    assert claimed.status == JobStatus.RUNNING
    assert claimed.started_at == 200.0
    assert any("FOR UPDATE SKIP LOCKED" in sql for sql, _ in fake_conn.calls)
    assert fake_conn.calls[0][1][:2] == (
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
    )
