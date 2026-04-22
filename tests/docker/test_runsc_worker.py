from __future__ import annotations

import json
import shutil
import subprocess

import pytest

IMAGE = "clippyshot:dev"

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _docker_runtimes() -> set[str]:
    if shutil.which("docker") is None:
        return set()
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()
    if proc.returncode != 0:
        return set()
    try:
        parsed = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return set()
    if isinstance(parsed, dict):
        return {str(key).strip().lower() for key in parsed}
    if isinstance(parsed, list):
        return {str(item).strip().lower() for item in parsed}
    return set()



def _image_supports_worker(image: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", image, "worker", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0

def _image_exists(image: str) -> bool:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


pytestmark = [pytest.mark.docker, needs_docker]


@pytest.mark.skipif("runsc" not in _docker_runtimes(), reason="runsc runtime not available in docker")
@pytest.mark.skipif(not _image_exists(IMAGE), reason="clippyshot:dev image not built")
@pytest.mark.skipif(not _image_supports_worker(IMAGE), reason="clippyshot:dev image predates worker command")
def test_worker_subcommand_is_invocable_under_runsc():
    r = subprocess.run(
        ["docker", "run", "--rm", "--runtime=runsc", IMAGE, "worker", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "--job-dir" in r.stdout
    assert "--input" in r.stdout
    assert "--output" in r.stdout
