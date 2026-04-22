from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from clippyshot.dispatcher import Dispatcher
from clippyshot.jobs import InMemoryJobStore, Job, JobStatus
from clippyshot.runtime.docker_runtime import DockerRuntimeSelection


def _make_job(tmp_path: Path, filename: str = "sample.docx") -> Job:
    job = Job.new(filename=filename)
    job_dir = tmp_path / "storage" / "jobs" / job.job_id
    input_dir = job_dir / "input"
    output_dir = job_dir / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / filename).write_bytes(b"payload")
    job.result_dir = str(output_dir)
    return job


def test_dispatcher_claims_launches_and_updates_success(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path)
    store.create(job)

    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])
    captured = {}

    def runtime_selector():
        return runtime

    def runner(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        assert len(mounts) == 2
        input_src = None
        output_src = None
        for mount in mounts:
            if "dst=/tmp/output" in mount:
                for part in mount.split(","):
                    if part.startswith("src="):
                        output_src = Path(part.removeprefix("src="))
            if "dst=/tmp/input/" in mount:
                for part in mount.split(","):
                    if part.startswith("src="):
                        input_src = Path(part.removeprefix("src="))
        assert input_src is not None
        assert output_src is not None
        assert input_src.name == job.filename
        out_dir = output_src
        out_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "pages": [
                {
                    "index": 1,
                    "file": "page-001.png",
                    "qr": [],
                    "ocr": {"text": "", "char_count": 0, "duration_ms": 0},
                }
            ],
            "render": {
                "page_count_rendered": 1,
                "page_count_total": 1,
                "scanners": {
                    "qr": {"enabled": True},
                    "ocr": {"enabled": True},
                },
            },
        }
        (out_dir / "metadata.json").write_text(json.dumps(metadata))
        (out_dir / "page-001.png").write_bytes(b"png")
        return subprocess.CompletedProcess(argv, 0, stdout="done", stderr="")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=runtime_selector,
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    processed = dispatcher.dispatch_once()

    updated = store.get(job.job_id)
    assert processed is True
    assert updated is not None
    assert updated.status is JobStatus.DONE
    assert updated.worker_runtime == "runsc"
    assert updated.security_warnings == []
    assert updated.pages_done == 1
    assert updated.pages_total == 1
    assert updated.result_dir == job.result_dir
    assert updated.finished_at is not None
    assert captured["argv"][0:3] == ["docker", "run", "--rm"]
    assert "--runtime=runsc" in captured["argv"]
    assert captured["argv"].count("--mount") == 2


def test_dispatcher_passes_job_scan_options_to_worker_env(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path)
    job.scan_options = {
        "CLIPPYSHOT_ENABLE_QR": "0",
        "CLIPPYSHOT_ENABLE_OCR": "1",
        "CLIPPYSHOT_OCR_LANG": "eng+deu",
    }
    store.create(job)

    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = argv
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        output_src = None
        for mount in mounts:
            if "dst=/tmp/output" in mount:
                for part in mount.split(","):
                    if part.startswith("src="):
                        output_src = Path(part.removeprefix("src="))
        assert output_src is not None
        output_src.mkdir(parents=True, exist_ok=True)
        (output_src / "metadata.json").write_text(
            json.dumps(
                {
                    "pages": [],
                    "render": {
                        "scanners": {"qr": {"enabled": True}, "ocr": {"enabled": True}}
                    },
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=lambda: runtime,
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    assert dispatcher.dispatch_once() is True
    env_pairs = []
    for i in range(len(captured["argv"]) - 1):
        if captured["argv"][i] == "-e":
            name, _, value = captured["argv"][i + 1].partition("=")
            env_pairs.append((name, value))

    assert ("CLIPPYSHOT_ENABLE_QR", "0") in env_pairs
    assert ("CLIPPYSHOT_ENABLE_OCR", "1") in env_pairs
    assert ("CLIPPYSHOT_OCR_LANG", "eng+deu") in env_pairs


def test_dispatcher_records_runtime_fallback_warning(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path, filename="fallback.docx")
    store.create(job)

    runtime = DockerRuntimeSelection(
        runtime="runc",
        secure=False,
        warnings=["runsc unavailable; falling back to runc"],
    )

    def runtime_selector():
        return runtime

    def runner(argv, **kwargs):
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        output_src = None
        for mount in mounts:
            if "dst=/tmp/output" in mount:
                for part in mount.split(","):
                    if part.startswith("src="):
                        output_src = Path(part.removeprefix("src="))
        assert output_src is not None
        out_dir = output_src
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "pages": [],
                    "render": {
                        "scanners": {"qr": {"enabled": True}, "ocr": {"enabled": True}}
                    },
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=runtime_selector,
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    assert dispatcher.dispatch_once() is True
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.worker_runtime == "runc"
    assert updated.security_warnings == ["runsc unavailable; falling back to runc"]
    assert updated.status is JobStatus.DONE


def test_dispatcher_marks_failed_job_from_nonzero_exit(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path, filename="broken.docx")
    store.create(job)

    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])

    def runtime_selector():
        return runtime

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 17, stdout="", stderr="boom")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=runtime_selector,
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    assert dispatcher.dispatch_once() is True
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.status is JobStatus.FAILED
    assert updated.finished_at is not None
    assert updated.worker_runtime == "runsc"
    assert updated.error is not None
    assert "exit 17" in updated.error
    assert "boom" in updated.error


def test_dispatcher_fails_success_exit_without_metadata(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path, filename="nometa.docx")
    store.create(job)

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=lambda: DockerRuntimeSelection(
            runtime="runsc", secure=True, warnings=[]
        ),
        subprocess_runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr=""
        ),
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    assert dispatcher.dispatch_once() is True
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.status is JobStatus.FAILED
    assert (
        updated.error
        == "worker exited successfully but metadata.json was missing or invalid"
    )


def test_dispatcher_returns_false_when_no_jobs():
    dispatcher = Dispatcher(
        job_store=InMemoryJobStore(),
        image_name="clippyshot-worker:dev",
        runtime_selector=lambda: DockerRuntimeSelection(
            runtime="runsc", secure=True, warnings=[]
        ),
        subprocess_runner=lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr=""
        ),
    )

    assert dispatcher.dispatch_once() is False


def test_dispatcher_discovers_host_storage_root_from_its_own_mounts(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOSTNAME", "dispatcher-test")

    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"]:
            payload = '[{"Destination": "/var/lib/clippyshot", "Source": "/host/clippy-data"}]'
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    dispatcher = Dispatcher(
        job_store=InMemoryJobStore(),
        image_name="clippyshot-worker:dev",
        subprocess_runner=runner,
    )
    path = dispatcher._resolve_host_path(
        Path("/var/lib/clippyshot/jobs/abc/input/tiny.docx")
    )
    assert path == Path("/host/clippy-data/jobs/abc/input/tiny.docx")


def test_dispatcher_run_forever_starts_up_to_configured_parallelism(tmp_path: Path):
    store = InMemoryJobStore()
    jobs = [_make_job(tmp_path, filename=f"doc-{idx}.docx") for idx in range(3)]
    for job in jobs:
        store.create(job)

    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])
    started = threading.Event()
    release = threading.Event()
    active_lock = threading.Lock()
    active = 0
    max_seen = 0

    def runner(argv, **kwargs):
        nonlocal active, max_seen
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--mount"]
        output_src = None
        for mount in mounts:
            if "dst=/tmp/output" in mount:
                for part in mount.split(","):
                    if part.startswith("src="):
                        output_src = Path(part.removeprefix("src="))
        assert output_src is not None
        with active_lock:
            active += 1
            max_seen = max(max_seen, active)
            if active >= 2:
                started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("worker was never released")
        output_src.mkdir(parents=True, exist_ok=True)
        (output_src / "metadata.json").write_text(json.dumps({"pages": []}))
        with active_lock:
            active -= 1
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        runtime_selector=lambda: runtime,
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
        max_parallel_jobs=2,
    )

    thread = threading.Thread(
        target=dispatcher.run_forever, kwargs={"poll_interval_s": 0.01}, daemon=True
    )
    thread.start()

    assert started.wait(timeout=1), "dispatcher never reached parallel worker execution"
    release.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        done = sum(1 for job in jobs if store.get(job.job_id).status is JobStatus.DONE)
        if done >= 2:
            break
        time.sleep(0.01)

    assert max_seen == 2


def test_dispatcher_requeues_running_job_without_live_worker_container(tmp_path: Path):
    store = InMemoryJobStore()
    job = _make_job(tmp_path, filename="stale.docx")
    store.create(job)
    store.update(
        job.job_id, status=JobStatus.RUNNING, started_at=123.0, worker_runtime="runsc"
    )

    def runner(argv, **kwargs):
        if argv[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    dispatcher = Dispatcher(
        job_store=store,
        image_name="clippyshot-worker:dev",
        subprocess_runner=runner,
        job_root=tmp_path / "storage" / "jobs",
        storage_root=tmp_path / "storage",
    )

    assert dispatcher._requeue_orphaned_jobs() == 1
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.status is JobStatus.QUEUED
    assert updated.started_at is None
    assert updated.worker_runtime is None
