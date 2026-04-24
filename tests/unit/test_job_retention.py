from pathlib import Path

from clippyshot.jobs import (
    InMemoryJobStore,
    Job,
    JobArtifactRegistry,
    JobStatus,
    SqlJobStore,
)


def test_registry_expires_finished_job_artifacts(tmp_path: Path):
    store = InMemoryJobStore()
    job = Job.new(filename="a.docx")
    store.create(job)

    work = tmp_path / "job-1"
    out = work / "out"
    out.mkdir(parents=True)
    (out / "page-001.png").write_bytes(b"png")

    registry = JobArtifactRegistry(retention_seconds=10, clock=lambda: 100.0)
    registry.register(job.job_id, out)
    registry.mark_finished(job.job_id)
    store.update(job.job_id, status=JobStatus.DONE, result_dir=str(out))

    registry.expire_due(store)
    assert out.exists()

    registry._clock = lambda: 111.0  # noqa: SLF001
    expired = registry.expire_due(store)

    assert expired == [job.job_id]
    assert not work.exists()
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.EXPIRED
    assert updated.result_dir is None


def test_registry_delete_removes_job_directory_immediately(tmp_path: Path):
    work = tmp_path / "job-2"
    out = work / "out"
    out.mkdir(parents=True)

    registry = JobArtifactRegistry(retention_seconds=10)
    registry.register("job-2", out)

    registry.delete("job-2")

    assert not work.exists()
    assert registry.path_for("job-2") is None


def test_registry_non_expiring_jobs_do_not_expire_when_retention_is_zero(
    tmp_path: Path,
):
    store = InMemoryJobStore()
    job = Job.new(filename="keep.docx")
    store.create(job)

    work = tmp_path / "job-keep"
    out = work / "out"
    out.mkdir(parents=True)

    registry = JobArtifactRegistry(retention_seconds=0, clock=lambda: 100.0)
    registry.register(job.job_id, out)
    registry.mark_finished(job.job_id)
    store.update(job.job_id, status=JobStatus.DONE, result_dir=str(out))

    expired = registry.expire_due(store)

    assert expired == []
    assert work.exists()
    assert store.get(job.job_id).status == JobStatus.DONE


def test_registry_expires_persisted_job_without_in_memory_registration(tmp_path: Path):
    db_url = f"sqlite:///{tmp_path / 'jobs.db'}"
    store = SqlJobStore(db_url)
    job = Job.new(filename="persisted.docx")
    store.create(job)

    work = tmp_path / "job-3"
    out = work / "out"
    out.mkdir(parents=True)
    (out / "page-001.png").write_bytes(b"png")
    store.update(
        job.job_id,
        status=JobStatus.DONE,
        result_dir=str(out),
        expires_at=90.0,
    )

    registry = JobArtifactRegistry(retention_seconds=10, clock=lambda: 100.0)
    expired = registry.expire_due(store)

    assert expired == [job.job_id]
    assert not work.exists()
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.EXPIRED
    assert updated.result_dir is None
