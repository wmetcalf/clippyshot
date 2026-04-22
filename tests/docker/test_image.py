import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
IMAGE = "clippyshot:dev"

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _container_sandbox_works() -> bool:
    """Probe whether the Docker image can actually create user namespaces.

    On Ubuntu 24.04+ hosts where the kernel restricts unprivileged user
    namespaces and the clippyshot-bwrap/clippyshot-nsjail AppArmor profiles
    are not loaded, even nsjail-inside-container will fail to create the
    namespaces it needs. This probe runs the selftest (which now checks
    the smoketest exit code) and returns False if it fails.
    """
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "run", "--rm",
             "--read-only",
             "--cap-drop=ALL",
             "--security-opt=no-new-privileges",
             "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
             "--tmpfs", "/var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001",
             IMAGE, "selftest"],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


_CONTAINER_SANDBOX_REASON = (
    "container sandbox cannot create user namespaces — "
    "load deploy/apparmor/clippyshot-bwrap and clippyshot-nsjail on the host "
    "(see deploy/apparmor/README.md)"
)

needs_container_sandbox = pytest.mark.skipif(
    not _container_sandbox_works(),
    reason=_CONTAINER_SANDBOX_REASON,
)

pytestmark = [pytest.mark.docker, needs_docker]


def _run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


@needs_container_sandbox
def test_selftest_passes_in_locked_down_runtime():
    """Verify the image runs the healthcheck under production-like flags."""
    r = _run([
        "docker", "run", "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
        "--tmpfs", "/var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001",
        IMAGE, "selftest",
    ])
    assert r.returncode == 0, f"selftest failed: stdout={r.stdout}\nstderr={r.stderr}"


@needs_container_sandbox
def test_convert_real_docx_in_container(tmp_path: Path):
    """End-to-end conversion of a real docx through the Docker image."""
    fixture = REPO / "tests" / "fixtures" / "safe" / "fixture.docx"
    if not fixture.exists():
        pytest.skip("safe fixture fixture.docx not built")
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy2(fixture, work / "input.docx")
    out = work / "out"
    out.mkdir()
    out.chmod(0o777)

    r = _run([
        "docker", "run", "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
        "--tmpfs", "/var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001",
        "-v", f"{work}:/work",
        "--user", str(os.getuid()),
        IMAGE, "convert", "/work/input.docx", "-o", "/work/out", "--quiet",
    ])
    assert r.returncode == 0, f"convert failed: stdout={r.stdout}\nstderr={r.stderr}"
    assert (out / "metadata.json").exists()
    assert (out / "page-001.png").exists()


@needs_container_sandbox
def test_convert_rejects_spoofed_input(tmp_path: Path):
    """spoofed.docx (PDF bytes with .docx extension) should exit 2."""
    fixture = REPO / "tests" / "fixtures" / "safe" / "spoofed.docx"
    if not fixture.exists():
        pytest.skip("spoofed fixture not present")
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy2(fixture, work / "input.docx")
    out = work / "out"
    out.mkdir()

    r = _run([
        "docker", "run", "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
        "--tmpfs", "/var/lib/clippyshot:rw,nosuid,size=64m,uid=10001,gid=10001",
        "-v", f"{work}:/work",
        "--user", str(os.getuid()),
        IMAGE, "convert", "/work/input.docx", "-o", "/work/out",
    ])
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}: stderr={r.stderr}"
