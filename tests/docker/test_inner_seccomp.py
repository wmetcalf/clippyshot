"""In the BUILT image, ClippyShot's own nsjail and bwrap sandboxes enforce their seccomp filter.

Shipped defect (found in the blastbox 0.1.51 rollout; true of older images too): inside the
image neither inner sandbox had a working filter.

* nsjail: its bundled kafel has no `io_uring_*` names, so the KAFEL policy failed to compile
  and nsjail exited 255 ("Couldn't prepare sandboxing policy") on EVERY run -- it failed
  closed, but the backend still reported `seccomp_active`.
* bwrap: the BPF builder imported libseccomp's Python bindings, a distro package the image's
  venv cannot see, so bwrap ran with NO filter.

Unit tests could not see either: one depends on the nsjail actually compiled into the image,
the other on the image's Python environment. So this runs ClippyShot's own backends inside the
image and asks the kernel. The outer container is started with `seccomp=unconfined` on
purpose: Docker's profile denies several of the same calls, and an EPERM it produced would
hide a missing inner filter. A control run outside the sandbox proves the calls are NOT denied
there, so an EPERM inside is the sandbox's.

Build the image first (`docker build --network=host -f deploy/docker/Dockerfile -t
clippyshot:dev .`); CLIPPYSHOT_TEST_IMAGE overrides the tag. The host needs unprivileged user
namespaces for the sandboxes (on Ubuntu: load deploy/apparmor/clippyshot-{bwrap,nsjail}).
With CLIPPYSHOT_REQUIRE_DOCKER_TESTS=1 (CI), a missing docker/image is a failure, not a skip.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess

import pytest

IMAGE = os.environ.get("CLIPPYSHOT_TEST_IMAGE", "clippyshot:dev")
REQUIRED = os.environ.get("CLIPPYSHOT_REQUIRE_DOCKER_TESTS", "") not in ("", "0")

pytestmark = pytest.mark.docker


def _unavailable(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"{reason} (CLIPPYSHOT_REQUIRE_DOCKER_TESTS=1)")
    pytest.skip(reason)


@pytest.fixture(scope="module", autouse=True)
def _image() -> None:
    if shutil.which("docker") is None:
        _unavailable("docker not installed")
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    if r.returncode != 0:
        _unavailable(f"image {IMAGE} not built")


# Calls from the denylist, with arguments under which an UNFILTERED unprivileged caller gets
# something other than EPERM (unshare(0) and kcmp(self, self, KCMP_VM) succeed, open_tree on
# "" is ENOENT, io_uring_setup(0, NULL) is EFAULT). x86_64 numbers: the image is x86_64-only.
_PROBE = r'''
import ctypes, json, os
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
pid = os.getpid()
calls = {
    "unshare": (272, 0),
    "open_tree": (428, -1, b"", 0),
    "kcmp": (312, pid, pid, 1, 0, 0),
    "io_uring_setup": (425, 0, None),
    "io_uring_enter": (426, -1, 0, 0, 0, None, 0),
    "mount": (165, None, None, None, 0, None),
}
out = {}
for name, (nr, *args) in calls.items():
    ctypes.set_errno(0)
    r = libc.syscall(nr, *args)
    out[name] = "ok" if r >= 0 else ctypes.get_errno()
print(json.dumps(out))
'''

# Runs inside the image under its own venv python. Uses only the backends' public run(), which
# exists on every version, so the same script measures the broken image and the fixed one.
_DRIVER = r'''
import json, subprocess, sys
from clippyshot.limits import Limits
from clippyshot.sandbox.base import SandboxRequest

PROBE = sys.stdin.read()
mode = sys.argv[1]
req = SandboxRequest(argv=["/usr/bin/python3", "-I", "-S", "-c", PROBE],
                     limits=Limits(timeout_s=60), attach_apparmor=False)

def report(**kw):
    print("RESULT " + json.dumps(kw))

if mode == "outside":
    r = subprocess.run(["/usr/bin/python3", "-I", "-S", "-c", PROBE], capture_output=True)
    report(exit=r.returncode, stdout=r.stdout.decode(), stderr=r.stderr.decode()[-500:])
    sys.exit()

backend, variant = mode.split(":")
if backend == "nsjail":
    from clippyshot.sandbox.nsjail import NsjailSandbox, _find_seccomp_policy
    policy = _find_seccomp_policy()
    if variant == "broken":
        # The shipped defect, reconstructed: a policy naming a syscall kafel cannot resolve.
        bad = "/tmp/broken.policy"
        with open(bad, "w") as f:
            f.write("POLICY p { ERRNO(1) { io_uring_setup } } USE p DEFAULT ALLOW\n")
        policy = __import__("pathlib").Path(bad)
    sb = NsjailSandbox(seccomp_policy=policy)
else:
    import clippyshot.sandbox.seccomp_denylist as d
    if variant == "broken":
        # A program that installs fine and denies nothing: `ret SECCOMP_RET_ALLOW`.
        d.build_bpf_bytes = lambda: bytes.fromhex("0600000000 00ff7f".replace(" ", ""))
    from clippyshot.sandbox.bwrap import BwrapSandbox
    sb = BwrapSandbox()
try:
    r = sb.run(req)
except Exception as e:
    report(raised=type(e).__name__, error=str(e)[-500:], seccomp_active=sb.seccomp_active)
    sys.exit()
report(exit=r.exit_code, stdout=r.stdout.decode(), stderr=r.stderr.decode()[-500:],
       seccomp_active=sb.seccomp_active)
'''


def _in_image(mode: str) -> dict:
    driver = _DRIVER
    r = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            # Deliberately NO outer seccomp: its EPERMs would mask a missing inner filter.
            "--security-opt", "seccomp=unconfined",
            # Unconfined, so the host's clippyshot-{bwrap,nsjail} profiles attach by path
            # and the sandboxes may create user namespaces; a fresh procfs needs systempaths.
            "--security-opt", "apparmor=unconfined",
            "--security-opt", "systempaths=unconfined",
            "--network=none",
            "--entrypoint", "/opt/clippyshot/bin/python",
            IMAGE, "-c", driver, mode,
        ],
        input=_PROBE, capture_output=True, text=True, timeout=300,
    )
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    pytest.fail(f"no result from the image ({mode}): rc={r.returncode}\n"
                f"stdout={r.stdout[-1000:]}\nstderr={r.stderr[-2000:]}")


def _calls(res: dict) -> dict:
    assert res.get("exit") == 0, f"probe did not run: {res}"
    return json.loads(res["stdout"].strip().splitlines()[-1])


@pytest.fixture(scope="module")
def outside() -> dict:
    """Control: with no inner sandbox, the probed calls are NOT denied in this container."""
    got = _calls(_in_image("outside"))
    assert any(v != errno.EPERM for v in got.values()), (
        f"every probed call is already EPERM outside the sandbox, so this test cannot see "
        f"the inner filter: {got}")
    return got


@pytest.mark.parametrize("backend", ["nsjail", "bwrap"])
def test_denied_syscalls_are_eperm_inside_the_sandbox(backend: str, outside: dict) -> None:
    res = _in_image(f"{backend}:shipped")
    assert "raised" not in res, f"{backend} refused to run with the shipped filter: {res}"
    inside = _calls(res)
    leaked = {k: v for k, v in inside.items() if v != errno.EPERM}
    assert not leaked, (
        f"inside {backend} these were not EPERM ('ok' = it succeeded): {leaked}; "
        f"outside: {outside}")
    assert res["seccomp_active"] is True


@pytest.mark.parametrize("backend", ["nsjail", "bwrap"])
def test_a_filter_that_does_not_apply_fails_closed(backend: str, outside: dict) -> None:
    """Break the filter and the backend must REFUSE, not run the request unfiltered (bwrap)
    or fail it as an anonymous exit 255 while still claiming a filter (nsjail)."""
    res = _in_image(f"{backend}:broken")
    assert res.get("raised") == "SeccompNotEnforced", (
        f"{backend} ran with a broken filter instead of refusing: {res}")
    assert res["seccomp_active"] is False


def test_the_image_selftest_selects_a_filtered_nsjail() -> None:
    """The startup self-check path: `clippyshot selftest` (the image HEALTHCHECK) selects the
    backend through select_sandbox, whose smoketest now proves the filter."""
    r = subprocess.run(
        ["docker", "run", "--rm",
         "--security-opt", "seccomp=unconfined",
         "--security-opt", "apparmor=unconfined",
         "--security-opt", "systempaths=unconfined",
         "--network=none",
         "-e", "CLIPPYSHOT_SANDBOX=nsjail",
         IMAGE, "clippyshot", "selftest"],
        capture_output=True, text=True, timeout=300,
    )
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    assert "clippyshot-nsjail" in out, out[-3000:]
