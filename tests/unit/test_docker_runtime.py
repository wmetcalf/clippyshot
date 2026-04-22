from pathlib import Path
import logging

from clippyshot.runtime.docker_runtime import (
    DockerRuntimeSelection,
    build_worker_docker_run_argv,
    select_worker_runtime,
)


def test_prefers_runsc_when_available():
    runtime = select_worker_runtime(available_runtimes={"runsc", "runc"})

    assert runtime.runtime == "runsc"
    assert runtime.secure is True
    assert runtime.warnings == []


def test_falls_back_to_runc_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="clippyshot.runtime.docker_runtime"):
        runtime = select_worker_runtime(available_runtimes={"runc"})

    assert runtime.runtime == "runc"
    assert runtime.secure is False
    assert runtime.warnings == ["runsc unavailable; falling back to runc"]
    assert "runsc unavailable; falling back to runc" in caplog.text


def test_builds_narrow_docker_run_argv_for_single_job():
    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])

    argv = build_worker_docker_run_argv(
        image="clippyshot-worker:dev",
        input_path=Path("/var/lib/clippyshot/jobs/abc123/input/sample.docx"),
        input_mount_path="/tmp/input/sample.docx",
        output_dir=Path("/var/lib/clippyshot/jobs/abc123/output"),
        output_mount_path="/tmp/output",
        worker_argv=[
            "worker",
            "--job-dir",
            "/tmp",
            "--input",
            "/tmp/input/sample.docx",
            "--output",
            "/tmp/output",
        ],
        runtime=runtime,
    )

    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--runtime=runsc" in argv
    assert "--user" in argv
    assert "10001:10001" in argv
    assert "-e" in argv
    env_spec = argv[argv.index("-e") + 1]
    assert env_spec == "CLIPPYSHOT_WARN_ON_INSECURE=1"
    assert argv.count("--mount") == 2
    input_mount = argv[argv.index("--mount") + 1]
    output_mount = argv[argv.index("--mount", argv.index("--mount") + 1) + 1]
    assert input_mount.startswith("type=bind")
    assert "src=/var/lib/clippyshot/jobs/abc123/input/sample.docx" in input_mount
    assert "dst=/tmp/input/sample.docx" in input_mount
    assert "readonly" in input_mount
    assert output_mount.startswith("type=bind")
    assert "src=/var/lib/clippyshot/jobs/abc123/output" in output_mount
    assert "dst=/tmp/output" in output_mount
    assert "readonly" not in output_mount and ",ro" not in output_mount
    assert "--entrypoint" not in argv

    image_index = argv.index("clippyshot-worker:dev")
    assert argv[image_index + 1 :] == [
        "worker",
        "--job-dir",
        "/tmp",
        "--input",
        "/tmp/input/sample.docx",
        "--output",
        "/tmp/output",
    ]


def test_scanner_env_propagated(monkeypatch):
    """Verify dispatcher propagates scanner env vars to worker docker run argv."""
    monkeypatch.setenv("CLIPPYSHOT_ENABLE_OCR", "1")
    monkeypatch.setenv("CLIPPYSHOT_OCR_LANG", "eng+deu")
    monkeypatch.delenv(
        "CLIPPYSHOT_ENABLE_QR", raising=False
    )  # unset propagates nothing
    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])
    argv = build_worker_docker_run_argv(
        image="clippyshot:test",
        input_path=Path("/tmp/job/input/x.docx"),
        input_mount_path="/tmp/input/x.docx",
        output_dir=Path("/tmp/job/output"),
        output_mount_path="/tmp/output",
        worker_argv=["worker", "--job-dir", "/tmp"],
        runtime=runtime,
    )
    # Turn argv into (key, value) pairs for the -e flags
    env_pairs = []
    for i in range(len(argv) - 1):
        if argv[i] == "-e":
            name, _, val = argv[i + 1].partition("=")
            env_pairs.append((name, val))
    names = {k for k, _ in env_pairs}
    assert "CLIPPYSHOT_ENABLE_OCR" in names
    assert ("CLIPPYSHOT_OCR_LANG", "eng+deu") in env_pairs
    assert "CLIPPYSHOT_ENABLE_QR" not in names  # unset → not propagated


def test_extra_env_overrides_per_job_scanner_settings():
    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])

    argv = build_worker_docker_run_argv(
        image="clippyshot:test",
        input_path=Path("/tmp/job/input/x.docx"),
        input_mount_path="/tmp/input/x.docx",
        output_dir=Path("/tmp/job/output"),
        output_mount_path="/tmp/output",
        worker_argv=["worker", "--job-dir", "/tmp"],
        runtime=runtime,
        extra_env={"CLIPPYSHOT_ENABLE_QR": "0", "CLIPPYSHOT_OCR_LANG": "eng+deu"},
    )

    env_pairs = []
    for i in range(len(argv) - 1):
        if argv[i] == "-e":
            name, _, val = argv[i + 1].partition("=")
            env_pairs.append((name, val))

    assert ("CLIPPYSHOT_ENABLE_QR", "0") in env_pairs
    assert ("CLIPPYSHOT_OCR_LANG", "eng+deu") in env_pairs


def test_builds_docker_run_argv_with_job_labels():
    runtime = DockerRuntimeSelection(runtime="runsc", secure=True, warnings=[])

    argv = build_worker_docker_run_argv(
        image="clippyshot-worker:dev",
        input_path=Path("/var/lib/clippyshot/jobs/abc123/input/sample.docx"),
        input_mount_path="/tmp/input/sample.docx",
        output_dir=Path("/var/lib/clippyshot/jobs/abc123/output"),
        output_mount_path="/tmp/output",
        worker_argv=["worker"],
        runtime=runtime,
        container_name="clippyshot-worker-abc123",
        labels={"clippyshot.role": "worker", "clippyshot.job_id": "abc123"},
    )

    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "clippyshot-worker-abc123"
    labels = [argv[i + 1] for i, part in enumerate(argv) if part == "--label"]
    assert "clippyshot.role=worker" in labels
    assert "clippyshot.job_id=abc123" in labels
