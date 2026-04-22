from pathlib import Path


def test_run_tests_uses_writable_tmpfs_for_clippy_user():
    script = Path("run_tests.sh").read_text()

    assert "--tmpfs /var/lib/clippyshot:rw,nosuid,size=1g,uid=10001,gid=10001" in script
    assert '-F "file=@\\"${file}\\""' in script


def test_readme_describes_api_dispatcher_worker_split():
    readme = Path("README.md").read_text()

    assert "Docker Compose stack (recommended" in readme
    assert "`api` on `http://localhost:8001`" in readme
    assert "`dispatcher` — claims jobs from Postgres and launches one worker" in readme

    assert "API: uploads, job status, artifact serving, no Docker socket." in readme
    assert "Dispatcher: claims jobs, chooses `runsc`/`runc`, launches workers, has the Docker socket." in readme
    assert "Worker: one job, one mounted directory, no Postgres credentials." in readme

def test_readme_runtime_example_sets_tmpfs_owner_for_db():
    readme = Path("README.md").read_text()

    assert "--tmpfs /var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001" in readme


def test_runtime_dockerfile_keeps_worker_invocation_available():
    dockerfile = Path("deploy/docker/Dockerfile").read_text()

    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "clippyshot"]' in dockerfile
    assert 'CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    assert "CLIPPYSHOT_JOB_ROOT=/var/lib/clippyshot/jobs" in dockerfile


def test_runtime_apparmor_profile_does_not_mix_exec_modifiers():
    profile = Path("deploy/apparmor/clippyshot-runtime").read_text()

    assert "/usr/bin/bwrap ix," not in profile
    assert "/usr/local/bin/nsjail ix," not in profile
    assert "/usr/bin/bwrap Px -> clippyshot-bwrap," in profile
    assert "/usr/local/bin/nsjail Px -> clippyshot-nsjail," in profile


def test_runtime_apparmor_profile_allows_container_entrypoint_chain():
    profile = Path("deploy/apparmor/clippyshot-runtime").read_text()

    assert "/usr/bin/tini ix," in profile
    assert "/opt/clippyshot/bin/clippyshot ix," in profile
    assert "/usr/bin/python3* ix," in profile
